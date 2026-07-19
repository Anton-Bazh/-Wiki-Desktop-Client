#!/usr/bin/env python3
"""Punto de entrada unico de la app (prototipo de frontend).

FastAPI es el unico puerto que el usuario ve, con tres zonas:
- `/` -- el hub: que repos hay conectados, accesos directos.
- `/_admin` -- conectar, seleccionar y quitar repositorios.
- `/wiki/...` -- el contenido en si, reenviado (proxy HTTP) al proceso
  interno `mkdocs serve` (`MKDOCS_HOST:MKDOCS_PORT`), que nunca se
  expone aparte -- ver `proxy_to_mkdocs()`.

"Acerca de" no es una pantalla propia: es un modal (`about_modal.html`,
glassmorphism) que vive incrustado en el shell y en el header de la
wiki -- ver `openAboutModal()`.

Multi-repo (simple, a proposito): cada repo conectado vive clonado en
`repos/<repo_id>/`. Solo uno esta "activo" a la vez -- `synced_docs/`
(el `docs_dir` fijo de `mkdocs.yml`) es un symlink que se repunta al
repo activo en cada seleccion, y `mkdocs serve` se reinicia para que
recoja el cambio (el watcher no detecta symlinks re-apuntados, mismo
problema documentado para el re-clone en 04 Pantalla de conexion). No
hay N procesos de mkdocs corriendo en paralelo: mas simple, y solo uno
se ve a la vez de todas formas.

El token de cada repo se guarda en el keychain nativo del SO (via
`keyring`, backend SecretService en Linux/gnome-keyring) bajo una clave
por repo -- nunca en `config.json` ni en disco en texto plano.

Prototipo: usa el `git` del sistema via subprocess, con el token
embebido en la URL de clone solo para el proceso hijo. La version final
(Tauri + git2-rs) pasara las credenciales por `RemoteCallbacks` en vez
de la URL, para no dejarlas visibles en la lista de procesos (`ps`) de
la maquina -- limitacion conocida de este prototipo, ver 02 Motores de
backend.md.
"""

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
import keyring
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mkdocs.commands.build import build as mkdocs_build_site
from mkdocs.config import load_config as mkdocs_load_config

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPOS_DIR = PROJECT_ROOT / "repos"
REPOS_DIR.mkdir(exist_ok=True)
SYNCED_DOCS = PROJECT_ROOT / "synced_docs"
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
PAGE_INDEX_PATH = Path(__file__).resolve().parent / "page_index.json"
MKDOCS_PID_PATH = Path(__file__).resolve().parent / "mkdocs.pid"
MKDOCS_HOST, MKDOCS_PORT = "127.0.0.1", 8765

KEYRING_SERVICE = "wiki-desktop-client"
LEGACY_TOKEN_KEY = "github-pat"  # esquema de un solo repo, pre-multi-repo

APP_VERSION = (PROJECT_ROOT / "VERSION").read_text().strip()

# Repo fijo con la documentacion de uso de la app (no del contenido de
# un equipo) -- distinto de los repos que el usuario conecta. Vacio
# hasta que el repo exista; el boton del hub cae a /_admin mientras
# tanto. Cuando exista, poner aqui su URL publica de GitHub.
DOCS_REPO_URL: str | None = None

# Resultado del sync automatico de arranque (None si no habia repo
# activo configurado todavia). Se muestra una vez en /_admin para que
# el usuario sepa que se refresco solo, sin tener que adivinar.
startup_sync: tuple[bool, str] | None = None

# Indice de paginas por repo, para el hub ("todos los repos, todas sus
# paginas, con buscador"). Se reconstruye con un `mkdocs build` real
# (mismos hooks/plugins que la wiki servida) cada vez que un repo se
# sincroniza, asi que las URLs coinciden exactamente con lo que
# terminara sirviendo /wiki/ una vez ese repo este activo. Cacheado en
# disco para no tener que reconstruir todo en cada arranque.
PAGE_INDEX: dict[str, list[dict]] = {}
if PAGE_INDEX_PATH.exists():
    PAGE_INDEX = json.loads(PAGE_INDEX_PATH.read_text())


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Al iniciar el proceso (lo mas parecido a "abrir la app" en este
    prototipo web), si ya hay un repo activo de una sesion anterior, se
    hace el pull automatico -- tal como lo pide la spec original, en vez
    de depender de que el usuario le de clic a Resincronizar."""
    global startup_sync
    config = read_config()
    active = get_active_repo(config)
    if active is not None:
        token = keyring.get_password(KEYRING_SERVICE, token_key(active["id"]))
        dest = REPOS_DIR / active["id"]
        ok, message = run_sync(active["repo_url"], token, dest)
        startup_sync = (ok, message)
        # Se levanta la wiki aunque el pull automatico falle (ej. sin
        # internet): sirve el contenido de la ultima sincronizacion buena
        # que haya en disco, en vez de dejar al usuario sin nada.
        if (dest / ".git").exists():
            set_active_symlink(active["id"])
            ensure_mkdocs_running()
            refresh_page_index(active["id"])

    # Los demas repos ya estan clonados en disco -- indexarlos no
    # necesita red, solo si todavia no se habian indexado nunca.
    for repo in config["repos"]:
        if repo["id"] not in PAGE_INDEX:
            refresh_page_index(repo["id"])
    yield


app = FastAPI(title="Wiki Desktop Client", lifespan=lifespan)
app.mount("/branding", StaticFiles(directory=str(PROJECT_ROOT / "branding")), name="branding")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
templates.env.globals["app_version"] = APP_VERSION


def _slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text or "repo"


def repo_id_for(repo_url: str) -> str:
    match = re.match(r"^https://github\.com/([^/]+)/([^/]+?)(\.git)?/?$", repo_url)
    if match:
        return _slugify(f"{match.group(1)}-{match.group(2)}")
    return _slugify(repo_url)


def token_key(repo_id: str) -> str:
    return f"github-pat:{repo_id}"


def default_config() -> dict:
    return {"repos": [], "active_id": None}


def read_config() -> dict:
    if not CONFIG_PATH.exists():
        return default_config()
    data = json.loads(CONFIG_PATH.read_text())
    if "repo_url" in data:
        return _migrate_legacy_config(data)
    return data


def write_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2))


def _migrate_legacy_config(data: dict) -> dict:
    """Sube un config.json de la version de un solo repo (`{"repo_url":
    ...}`) al esquema multi-repo, reutilizando el clon y el token que ya
    existan en disco en vez de forzar un re-clone."""
    repo_url = data["repo_url"]
    repo_id = repo_id_for(repo_url)
    dest = REPOS_DIR / repo_id
    if SYNCED_DOCS.exists() and not SYNCED_DOCS.is_symlink() and not dest.exists():
        SYNCED_DOCS.rename(dest)

    old_token = keyring.get_password(KEYRING_SERVICE, LEGACY_TOKEN_KEY)
    if old_token:
        keyring.set_password(KEYRING_SERVICE, token_key(repo_id), old_token)
        try:
            keyring.delete_password(KEYRING_SERVICE, LEGACY_TOKEN_KEY)
        except keyring.errors.PasswordDeleteError:
            pass

    config = {"repos": [{"id": repo_id, "repo_url": repo_url}], "active_id": repo_id}
    write_config(config)
    return config


def get_repo(config: dict, repo_id: str) -> dict | None:
    return next((r for r in config["repos"] if r["id"] == repo_id), None)


def get_active_repo(config: dict) -> dict | None:
    if config["active_id"] is None:
        return None
    return get_repo(config, config["active_id"])


def set_active_symlink(repo_id: str | None) -> None:
    """Repunta `synced_docs/` (el docs_dir fijo de mkdocs.yml) al repo
    activo. `repo_id=None` lo borra sin reemplazo (sin repo activo)."""
    if SYNCED_DOCS.is_symlink():
        SYNCED_DOCS.unlink()
    elif SYNCED_DOCS.exists():
        shutil.rmtree(SYNCED_DOCS)
    if repo_id is not None:
        SYNCED_DOCS.symlink_to(REPOS_DIR / repo_id, target_is_directory=True)


def repo_display_name(repo_url: str) -> str:
    """Nombre corto para mostrar (ej. 'QALPIX-DOC' desde
    'https://github.com/Anton-Bazh/QALPIX-DOC.git')."""
    match = re.match(r"^https://github\.com/[^/]+/([^/]+?)(\.git)?/?$", repo_url)
    return match.group(1) if match else repo_url


def repo_owner(repo_url: str) -> str:
    """Usuario u organizacion dueña del repo (ej. 'Anton-Bazh')."""
    match = re.match(r"^https://github\.com/([^/]+)/[^/]+?(\.git)?/?$", repo_url)
    return match.group(1) if match else ""


def repo_last_update(repo_id: str) -> str | None:
    """Fecha del ultimo commit en el clon local (ISO 8601), o None si no
    hay clon todavia. Se lee del propio `git log`, no de la API de
    GitHub: ya tenemos el repo clonado, así que no hace falta una
    llamada de red aparte (que ademas fallaria sin token en repos
    privados, o por rate limit si no hay token)."""
    dest = REPOS_DIR / repo_id
    if not (dest / ".git").exists():
        return None
    result = subprocess.run(
        ["git", "log", "-1", "--format=%cI"],
        cwd=dest,
        capture_output=True,
        text=True,
        timeout=10,
    )
    output = result.stdout.strip()
    return output if result.returncode == 0 and output else None


def relative_date(iso: str | None) -> str | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    now = datetime.now(dt.tzinfo or timezone.utc)
    days = (now - dt).days
    if days <= 0:
        return "hoy"
    if days == 1:
        return "ayer"
    if days < 7:
        return f"hace {days} días"
    if days < 30:
        n = days // 7
        return f"hace {n} semana{'s' if n != 1 else ''}"
    if days < 365:
        n = days // 30
        return f"hace {n} mes{'es' if n != 1 else ''}"
    n = days // 365
    return f"hace {n} año{'s' if n != 1 else ''}"


def build_page_index(repo_id: str) -> list[dict]:
    """Corre un `mkdocs build` real (mismos hooks/plugins que la wiki en
    vivo) contra el clon de `repo_id`, a un directorio temporal, y
    extrae {title, location} de cada pagina real de su
    `search_index.json` (se descartan las entradas de sub-encabezado,
    que traen '#' en la location). Asi las URLs del hub coinciden
    exactamente con lo que servira /wiki/ una vez que ese repo este
    activo -- no se reimplementa el slugify por separado."""
    dest = REPOS_DIR / repo_id
    if not (dest / ".git").exists():
        return []
    with tempfile.TemporaryDirectory() as tmp:
        try:
            config = mkdocs_load_config(
                str(PROJECT_ROOT / "mkdocs.yml"), docs_dir=str(dest), site_dir=tmp
            )
            mkdocs_build_site(config)
        except Exception:
            return []
        index_path = Path(tmp) / "search" / "search_index.json"
        if not index_path.exists():
            return []
        data = json.loads(index_path.read_text())

    pages = []
    for entry in data.get("docs", []):
        location = entry.get("location", "")
        if "#" in location:
            continue
        pages.append({"title": entry.get("title") or location, "location": location})
    return pages


def refresh_page_index(repo_id: str) -> None:
    PAGE_INDEX[repo_id] = build_page_index(repo_id)
    PAGE_INDEX_PATH.write_text(json.dumps(PAGE_INDEX, indent=2, ensure_ascii=False))


def scrub_token(text: str, token: str | None) -> str:
    if token:
        text = text.replace(token, "***")
    return text


def authenticated_url(repo_url: str, token: str | None) -> str:
    if not token:
        return repo_url
    match = re.match(r"^https://(.+)$", repo_url)
    if not match:
        return repo_url
    return f"https://{token}@{match.group(1)}"


def run_sync(repo_url: str, token: str | None, dest: Path) -> tuple[bool, str]:
    """Clona `dest` si no existe, o hace pull si ya existe."""
    clone_url = authenticated_url(repo_url, token)
    try:
        if not (dest / ".git").exists():
            result = subprocess.run(
                ["git", "clone", clone_url, str(dest)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        else:
            # Si cambio el token o la URL, se actualiza el remoto antes de tirar de el.
            subprocess.run(
                ["git", "remote", "set-url", "origin", clone_url],
                cwd=dest,
                capture_output=True,
                text=True,
                timeout=15,
            )
            result = subprocess.run(
                ["git", "pull", "--ff-only", "origin"],
                cwd=dest,
                capture_output=True,
                text=True,
                timeout=60,
            )
    except subprocess.TimeoutExpired:
        return False, "Tiempo de espera agotado conectando al repositorio."

    ok = result.returncode == 0
    output = scrub_token((result.stdout or "") + (result.stderr or ""), token)
    if ok:
        return True, "Sincronización correcta."
    return False, output.strip() or "Error desconocido al sincronizar."


def mkdocs_is_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((MKDOCS_HOST, MKDOCS_PORT)) == 0


def kill_mkdocs() -> None:
    """Mata el mkdocs serve que lanzamos nosotros (via pidfile propio).

    Necesario porque el watcher de archivos de `mkdocs serve` no detecta
    que `synced_docs/` cambio de destino (symlink re-apuntado o
    re-clone: nuevo inodo, mismo path): se queda sirviendo el build
    viejo indefinidamente. La unica forma confiable de servir el
    contenido nuevo es reiniciar el proceso, no reusar uno que ya
    estaba corriendo.
    """
    if not MKDOCS_PID_PATH.exists():
        return
    try:
        pid = int(MKDOCS_PID_PATH.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    finally:
        MKDOCS_PID_PATH.unlink(missing_ok=True)


def spawn_mkdocs() -> None:
    # --no-livereload: sin esto, mkdocs inyecta un script que abre un
    # websocket propio hacia su origen (MKDOCS_HOST:MKDOCS_PORT). Como
    # el unico puerto que el usuario ve es el de FastAPI (que hace de
    # proxy), ese websocket tendria que proxiarse tambien. Se desactiva
    # porque el contenido no cambia por edicion local en vivo, sino por
    # /resync o un cambio de repo activo, que ya reinician el proceso.
    proc = subprocess.Popen(
        [sys.executable, "-m", "mkdocs", "serve", "-a", f"{MKDOCS_HOST}:{MKDOCS_PORT}", "--no-livereload"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    MKDOCS_PID_PATH.write_text(str(proc.pid))
    for _ in range(20):
        if mkdocs_is_running():
            return
        time.sleep(0.3)


def ensure_mkdocs_running() -> None:
    """Arranca mkdocs solo si no hay nada escuchando en el puerto. Se usa
    tras un resync (pull en caliente sobre el repo activo), donde el
    watcher si funciona porque el symlink no cambio de destino."""
    if not mkdocs_is_running():
        spawn_mkdocs()


def restart_mkdocs() -> None:
    """Reinicia mkdocs siempre. Se usa cuando el repo activo cambia
    (conectar uno nuevo o seleccionar otro ya conectado), porque
    synced_docs/ paso a apuntar a un directorio distinto."""
    kill_mkdocs()
    spawn_mkdocs()


def render_admin(
    request: Request,
    config: dict,
    *,
    error: str | None = None,
    repo_url_prefill: str = "",
    sync_message: str | None = None,
    sync_ok: bool | None = None,
):
    repos = [
        {**r, "name": repo_display_name(r["repo_url"]), "owner": repo_owner(r["repo_url"])}
        for r in config["repos"]
    ]
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "repos": repos,
            "active_id": config["active_id"],
            "error": error,
            "repo_url": repo_url_prefill,
            "sync_message": sync_message,
            "sync_ok": sync_ok,
        },
    )


@app.get("/_admin", response_class=HTMLResponse)
def admin(request: Request):
    global startup_sync
    config = read_config()
    # El aviso del sync automatico de arranque se muestra una sola vez
    # (la primera visita a /_admin tras encender el proceso), no en
    # cada visita.
    sync_message = sync_ok = None
    if startup_sync is not None:
        sync_ok, base_message = startup_sync
        sync_message = ("Sincronizado al arrancar. " if sync_ok else "No se pudo sincronizar al arrancar (se muestra la última copia local). ") + base_message
        startup_sync = None
    return render_admin(request, config, sync_message=sync_message, sync_ok=sync_ok)


@app.post("/connect", response_class=HTMLResponse)
def connect(request: Request, repo_url: str = Form(...), token: str = Form("")):
    repo_url = repo_url.strip()
    token = token.strip() or None
    config = read_config()

    if not repo_url.startswith("https://github.com/"):
        return render_admin(
            request,
            config,
            error="Solo se soportan repositorios de GitHub (https://github.com/...) en esta versión.",
            repo_url_prefill=repo_url,
        )

    repo_id = repo_id_for(repo_url)
    existing = get_repo(config, repo_id)
    dest = REPOS_DIR / repo_id

    ok, message = run_sync(repo_url, token, dest)
    if not ok:
        return render_admin(request, config, error=message, repo_url_prefill=repo_url)

    if token:
        keyring.set_password(KEYRING_SERVICE, token_key(repo_id), token)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, token_key(repo_id))
        except keyring.errors.PasswordDeleteError:
            pass

    if existing is None:
        config["repos"].append({"id": repo_id, "repo_url": repo_url})
    config["active_id"] = repo_id
    write_config(config)

    set_active_symlink(repo_id)
    restart_mkdocs()
    refresh_page_index(repo_id)
    # A "/_admin", no a "/": el usuario necesita ver el resultado del
    # sync antes de que "/" empiece a servirle la wiki via proxy.
    return RedirectResponse("/_admin", status_code=303)


@app.post("/select/{repo_id}")
def select(repo_id: str):
    config = read_config()
    if get_repo(config, repo_id) is None:
        return RedirectResponse("/_admin", status_code=303)
    config["active_id"] = repo_id
    write_config(config)
    set_active_symlink(repo_id)
    restart_mkdocs()
    return RedirectResponse("/wiki", status_code=303)


@app.post("/resync", response_class=HTMLResponse)
def resync(request: Request):
    config = read_config()
    active = get_active_repo(config)
    if active is None:
        return RedirectResponse("/_admin", status_code=303)
    token = keyring.get_password(KEYRING_SERVICE, token_key(active["id"]))
    ok, message = run_sync(active["repo_url"], token, REPOS_DIR / active["id"])
    if ok:
        ensure_mkdocs_running()
        refresh_page_index(active["id"])
    return render_admin(request, config, sync_message=message, sync_ok=ok)


@app.post("/disconnect/{repo_id}")
def disconnect(repo_id: str):
    config = read_config()
    if get_repo(config, repo_id) is None:
        return RedirectResponse("/_admin", status_code=303)

    config["repos"] = [r for r in config["repos"] if r["id"] != repo_id]
    was_active = config["active_id"] == repo_id
    if was_active:
        config["active_id"] = None
    write_config(config)

    try:
        keyring.delete_password(KEYRING_SERVICE, token_key(repo_id))
    except keyring.errors.PasswordDeleteError:
        pass
    # Se borra para que una futura reconexion a esta URL parta de un
    # `clone` limpio -- un `pull` contra un remoto distinto fallaria
    # (historias no relacionadas).
    shutil.rmtree(REPOS_DIR / repo_id, ignore_errors=True)

    PAGE_INDEX.pop(repo_id, None)
    PAGE_INDEX_PATH.write_text(json.dumps(PAGE_INDEX, indent=2, ensure_ascii=False))

    if was_active:
        set_active_symlink(None)
        kill_mkdocs()

    return RedirectResponse("/_admin", status_code=303)


_HOP_BY_HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length"}


async def proxy_to_mkdocs(request: Request, path: str) -> Response:
    """Reenvia la request a `mkdocs serve` (interno, 127.0.0.1 only) y
    devuelve su respuesta tal cual. Este es el mecanismo que permite que
    la wiki se vea en el mismo puerto que el panel de administracion."""
    upstream_url = httpx.URL(
        f"http://{MKDOCS_HOST}:{MKDOCS_PORT}/{path}", params=request.query_params
    )
    headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", *_HOP_BY_HOP_HEADERS}}
    async with httpx.AsyncClient() as client:
        try:
            upstream = await client.request(
                request.method,
                upstream_url,
                headers=headers,
                content=await request.body(),
                timeout=10,
            )
        except httpx.ConnectError:
            return HTMLResponse(
                "<p>La wiki no esta disponible en este momento. "
                '<a href="/_admin">Ir a administracion</a> para revisar la conexion.</p>',
                status_code=503,
            )
    response_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP_HEADERS}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)


_BADGE_COUNT = 6


def badge_class(repo_id: str) -> str:
    """Color determinista por repo (siempre el mismo para el mismo repo,
    para poder reconocerlo de un vistazo entre tarjetas)."""
    return f"tag-{sum(ord(c) for c in repo_id) % _BADGE_COUNT + 1}"


@app.get("/", response_class=HTMLResponse)
def hub(request: Request):
    """El hub: una tarjeta por repo conectado (no por pagina), y accesos
    directos a las demas pantallas. Es lo primero que se ve siempre --
    ya no se salta directo a la wiki. El indice de paginas (PAGE_INDEX)
    no se muestra pagina por pagina aqui, pero sigue alimentando el
    conteo de paginas de cada tarjeta y el texto que el buscador filtra
    (asi "arquitectura" encuentra el repo aunque el nombre del repo no
    la mencione, porque alguna de sus paginas si)."""
    config = read_config()
    repos = []
    for r in config["repos"]:
        name = repo_display_name(r["repo_url"])
        pages = PAGE_INDEX.get(r["id"], [])
        search_blob = " ".join([name] + [p["title"] for p in pages]).lower()
        repos.append(
            {
                **r,
                "name": name,
                "owner": repo_owner(r["repo_url"]),
                "badge": badge_class(r["id"]),
                "page_count": len(pages),
                "last_update": relative_date(repo_last_update(r["id"])),
                "search": search_blob,
            }
        )
    return templates.TemplateResponse(
        request,
        "hub.html",
        {"repos": repos, "active_id": config["active_id"]},
    )

    return RedirectResponse(f"/wiki/{loc}", status_code=303)


@app.get("/_docs")
def docs():
    """Documentacion de uso de la app: la conecta (o la selecciona si ya
    estaba) y manda directo a /wiki. Sin DOCS_REPO_URL todavia, cae a
    /_admin -- no hay nada que mostrar. (No es "/docs": FastAPI ya usa
    esa ruta para su propio Swagger UI.)"""
    if not DOCS_REPO_URL:
        return RedirectResponse("/_admin", status_code=303)

    config = read_config()
    repo_id = repo_id_for(DOCS_REPO_URL)
    if get_repo(config, repo_id) is None:
        ok, _ = run_sync(DOCS_REPO_URL, None, REPOS_DIR / repo_id)
        if not ok:
            return RedirectResponse("/_admin", status_code=303)
        config["repos"].append({"id": repo_id, "repo_url": DOCS_REPO_URL})

    if config["active_id"] != repo_id:
        config["active_id"] = repo_id
        set_active_symlink(repo_id)
        restart_mkdocs()
    write_config(config)
    return RedirectResponse("/wiki", status_code=303)


@app.api_route("/wiki", methods=["GET", "POST", "HEAD"])
async def wiki_index(request: Request):
    if get_active_repo(read_config()) is None:
        return RedirectResponse("/", status_code=303)
    ensure_mkdocs_running()
    return await proxy_to_mkdocs(request, "wiki")


@app.api_route("/wiki/{path:path}", methods=["GET", "POST", "HEAD"])
async def wiki_proxy(request: Request, path: str):
    """Todo el contenido de la wiki (paginas, assets, search_index.json)
    vive bajo /wiki/ -- un prefijo fijo, no por repo, porque solo un
    repo esta activo a la vez (ver set_active_symlink). `mkdocs serve`
    ya sirve su contenido bajo ese mismo prefijo internamente (efecto de
    `site_url: http://.../wiki/` en mkdocs.yml), asi que se reenvia la
    ruta completa tal cual -- no hay que reescribir nada."""
    if get_active_repo(read_config()) is None:
        return RedirectResponse("/", status_code=303)
    ensure_mkdocs_running()
    return await proxy_to_mkdocs(request, f"wiki/{path}")
