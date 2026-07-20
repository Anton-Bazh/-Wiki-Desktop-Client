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
`repos/<repo_id>/`. Solo uno esta "activo" a la vez -- en cada seleccion
se genera `_runtime_mkdocs.yml` (copia de `mkdocs.yml` con `docs_dir`
apuntando directo a la ruta absoluta del repo activo, ver
`set_active_docs_dir()`) y `mkdocs serve` se reinicia para que lo
recoja. Sin symlink de por medio a proposito: Windows no crea symlinks
estilo Unix sin privilegios especiales, y el watcher de archivos de
mkdocs tampoco detectaba cuando un symlink cambiaba de destino -- las
dos razones desaparecen al no depender de symlinks en ninguna
plataforma. No hay N procesos de mkdocs corriendo en paralelo: mas
simple, y solo uno se ve a la vez de todas formas.

El token de cada repo se guarda en el keychain nativo del SO (via
`keyring`, backend SecretService en Linux/gnome-keyring, Credential
Locker en Windows) bajo una clave por repo -- nunca en `config.json` ni
en disco en texto plano.

Sincronizacion via `pygit2`/`libgit2` (clone/fetch + fast-forward
manual, ver `run_sync()`) -- no depende de que el usuario tenga `git`
instalado, y las credenciales pasan por `RemoteCallbacks` en vez de la
URL del proceso, para no dejarlas visibles en la lista de procesos
(`ps`) de la maquina.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import keyring
import pygit2
from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mkdocs.config import load_config as mkdocs_load_config
from mkdocs.structure.files import get_files
from mkdocs.structure.nav import get_navigation

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYNCED_DOCS = PROJECT_ROOT / "synced_docs"  # solo para migrar instalaciones viejas, ver _migrate_legacy_config


def _project_root_writable() -> bool:
    try:
        probe = PROJECT_ROOT / ".write_test"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


# Directorio donde vive el estado que la app escribe en caliente (config,
# indice de paginas, pid de mkdocs, clones de repos, config de mkdocs
# generado). En un checkout de desarrollo o en el instalable de Windows
# (que vive en `$LOCALAPPDATA`, ya de por si escribible por el usuario),
# es el propio `PROJECT_ROOT` -- mismas rutas que siempre (config.json
# junto a server.py, repos/ y _runtime_mkdocs.yml junto al mkdocs.yml
# fuente). El paquete `.deb` de Linux en cambio instala el codigo bajo
# `/opt/marc`, propiedad de root -- ahi PROJECT_ROOT no admite escritura,
# asi que todo el estado se mueve junto a `~/.local/share/marc` (XDG data
# dir), igual que cualquier otra app de escritorio en Linux separa
# binarios de estado de usuario.
if _project_root_writable():
    REPOS_DIR = PROJECT_ROOT / "repos"
    RUNTIME_MKDOCS_CONFIG = PROJECT_ROOT / "_runtime_mkdocs.yml"
    CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
    PAGE_INDEX_PATH = Path(__file__).resolve().parent / "page_index.json"
    MKDOCS_PID_PATH = Path(__file__).resolve().parent / "mkdocs.pid"
else:
    _STATE_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "marc"
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    REPOS_DIR = _STATE_DIR / "repos"
    RUNTIME_MKDOCS_CONFIG = _STATE_DIR / "_runtime_mkdocs.yml"
    CONFIG_PATH = _STATE_DIR / "config.json"
    PAGE_INDEX_PATH = _STATE_DIR / "page_index.json"
    MKDOCS_PID_PATH = _STATE_DIR / "mkdocs.pid"

REPOS_DIR.mkdir(exist_ok=True)
MKDOCS_HOST, MKDOCS_PORT = "127.0.0.1", 8765

KEYRING_SERVICE = "wiki-desktop-client"
LEGACY_TOKEN_KEY = "github-pat"  # esquema de un solo repo, pre-multi-repo

APP_VERSION = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()

# Repo fijo con la documentacion de uso de la app (no del contenido de
# un equipo) -- distinto de los repos que el usuario conecta. Vacio
# hasta que el repo exista; el boton del hub cae a /_admin mientras
# tanto. Cuando exista, poner aqui su URL publica de GitHub.
DOCS_REPO_URL: str | None = "https://github.com/Anton-Bazh/Wiki-Desktop-Client-doc.git"

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
    PAGE_INDEX = json.loads(PAGE_INDEX_PATH.read_text(encoding="utf-8"))

# Cache en memoria de la fecha del ultimo commit por repo, poblado junto
# con PAGE_INDEX (ver refresh_page_index). Sin esto, hub() invocaba un
# `git log` por repo en cada vista del Hub -- ahora solo se recalcula
# cuando el repo realmente se sincroniza.
LAST_UPDATE: dict[str, str | None] = {}

# Cliente HTTP compartido para el proxy hacia mkdocs (ver proxy_to_mkdocs),
# creado una vez en el lifespan. Una pagina de la wiki dispara decenas de
# requests (assets, search_index.json, etc.); un httpx.AsyncClient() nuevo
# por request pagaba una conexion TCP nueva cada vez en vez de reusar el
# pool de conexiones hacia 127.0.0.1.
HTTPX_CLIENT: httpx.AsyncClient | None = None

# Que repo esta sirviendo mkdocs ahora mismo -- en memoria, nunca en
# config.json. Separado a proposito de config["active_id"] (el repo que
# el usuario eligio, persistido): /_docs necesita apuntar mkdocs a la
# documentacion de uso sin que eso cuente como "el usuario cambio de
# repo" (ver docs() y ensure_serving()) -- antes /_docs sobreescribia
# active_id, y el Hub/Admin mostraban el repo real del usuario como
# "desactivado" mientras leia la ayuda.
SERVING_REPO_ID: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Al iniciar el proceso (lo mas parecido a "abrir la app" en este
    prototipo web), si ya hay un repo activo de una sesion anterior, se
    hace el pull automatico -- tal como lo pide la spec original, en vez
    de depender de que el usuario le de clic a Resincronizar."""
    global startup_sync, HTTPX_CLIENT, SERVING_REPO_ID
    HTTPX_CLIENT = httpx.AsyncClient()
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
            set_active_docs_dir(active["id"])
            ensure_mkdocs_running()
            SERVING_REPO_ID = active["id"]
            refresh_page_index(active["id"])

    # Los demas repos ya estan clonados en disco -- indexarlos no
    # necesita red, solo si todavia no se habian indexado nunca.
    for repo in config["repos"]:
        if repo["id"] not in PAGE_INDEX:
            refresh_page_index(repo["id"])
    yield
    await HTTPX_CLIENT.aclose()


app = FastAPI(title="MARC", lifespan=lifespan)
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
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if "repo_url" in data:
        return _migrate_legacy_config(data)
    return data


def write_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


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
    # El repo de documentacion de uso (DOCS_REPO_URL) nunca vive en
    # config["repos"] a proposito -- ver docs(). Si esta activo, se
    # sintetiza aqui su entrada para que el proxy de /wiki siga
    # funcionando igual que con un repo normal.
    if DOCS_REPO_URL and config["active_id"] == repo_id_for(DOCS_REPO_URL):
        return {"id": config["active_id"], "repo_url": DOCS_REPO_URL}
    return get_repo(config, config["active_id"])


def set_active_docs_dir(repo_id: str | None) -> None:
    """Genera `_runtime_mkdocs.yml` -- copia de `mkdocs.yml` con
    `docs_dir` apuntando directo a la ruta absoluta del repo activo.
    `repo_id=None` borra el archivo generado (sin repo activo).

    Reemplaza el symlink `synced_docs/` que usaba la version anterior:
    Windows no crea symlinks estilo Unix sin privilegios especiales
    (Modo de desarrollador/Administrador), y ademas `Path.is_symlink()`
    ni siquiera detecta de forma confiable un symlink ya creado en esa
    plataforma (confirmado corriendo bajo Windows/Wine: lo reporta como
    carpeta normal) -- se prefiere no depender de symlinks en absoluto,
    en ninguna plataforma. Mismo patron que ya usaba `build_page_index()`
    para overridear `docs_dir` via la API de Python, aqui aplicado al
    `mkdocs serve` que corre como subproceso via `-f/--config-file`.

    `custom_dir` (tema) y cada entrada de `hooks:` tambien son rutas
    relativas en `mkdocs.yml`, pero MkDocs las resuelve relativas a la
    carpeta del propio archivo de config, no a `PROJECT_ROOT` -- mientras
    `_runtime_mkdocs.yml` vivia siempre junto a `mkdocs.yml` (dentro de
    `PROJECT_ROOT`) esto pasaba desapercibido. Con el paquete `.deb` de
    Linux, `_runtime_mkdocs.yml` puede vivir en `STATE_DIR` (ver arriba,
    `/opt/marc` de solo lectura), separado de `theme_overrides/` y
    `hooks/`, que solo existen en `PROJECT_ROOT` -- sin este ajuste,
    `mkdocs serve` aborta con un error de configuracion antes de escuchar
    en `MKDOCS_PORT`, y el proxy nunca ve otra cosa que un connection
    refused (confirmado reproduciendo el paquete: `mkdocs serve` moria en
    el arranque, `/wiki` devolvia 503 con "La wiki no esta disponible")."""
    if repo_id is None:
        RUNTIME_MKDOCS_CONFIG.unlink(missing_ok=True)
        return
    base = (PROJECT_ROOT / "mkdocs.yml").read_text(encoding="utf-8")
    dest = (REPOS_DIR / repo_id).resolve()
    updated = re.sub(r"(?m)^docs_dir:.*$", f"docs_dir: {dest.as_posix()}", base)
    updated = re.sub(
        r"(?m)^(\s*custom_dir:\s*)theme_overrides\s*$",
        lambda m: f"{m.group(1)}{(PROJECT_ROOT / 'theme_overrides').as_posix()}",
        updated,
    )
    updated = re.sub(
        r"(?m)^(\s*-\s*)hooks/([a-zA-Z0-9_]+\.py)\s*$",
        lambda m: f"{m.group(1)}{(PROJECT_ROOT / 'hooks' / m.group(2)).as_posix()}",
        updated,
    )
    RUNTIME_MKDOCS_CONFIG.write_text(updated, encoding="utf-8")


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
    hay clon todavia. Se lee del propio repo con pygit2 (sin invocar el
    `git` del sistema), no de la API de GitHub: ya tenemos el repo
    clonado, así que no hace falta una llamada de red aparte (que ademas
    fallaria sin token en repos privados, o por rate limit si no hay
    token)."""
    dest = REPOS_DIR / repo_id
    if not (dest / ".git").exists():
        return None
    try:
        repo = pygit2.Repository(str(dest))
        commit = repo[repo.head.target]
    except (pygit2.GitError, KeyError):
        return None
    tz = timezone(timedelta(minutes=commit.commit_time_offset))
    return datetime.fromtimestamp(commit.commit_time, tz=tz).isoformat()


def cached_last_update(repo_id: str) -> str | None:
    """Como repo_last_update(), pero cacheado en LAST_UPDATE -- evita
    lanzar `git log` de nuevo en cada vista del Hub para repos que ya
    fueron consultados desde el ultimo sync."""
    if repo_id not in LAST_UPDATE:
        LAST_UPDATE[repo_id] = repo_last_update(repo_id)
    return LAST_UPDATE[repo_id]


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
    """Extrae {title, location} de cada pagina real del repo sin pagar un
    `mkdocs build` completo: arma el inventario de archivos (`get_files`),
    corre el evento `on_files` (asi `slugify_urls.py` normaliza las URLs
    igual que en la wiki servida) y arma la navegacion (`get_navigation`,
    que asigna cada `Page` a su `File`) -- pero nunca renderiza Markdown a
    HTML ni copia los assets del tema, que era el costo real de la version
    anterior (un `mkdocs build` completo solo para leer dos campos).
    `Page.title`, tras solo `read_source()`, ya resuelve el titulo con la
    misma prioridad que usaba el plugin de busqueda (meta 'title' -> primer
    H1 del Markdown crudo -> nombre de archivo), sin necesitar el render.
    Verificado contra el codigo fuente instalado que ninguno de los
    plugins activos (search, ezlinks, embed_file, callouts) depende de
    pasos posteriores a on_files/get_navigation para esto."""
    dest = REPOS_DIR / repo_id
    if not (dest / ".git").exists():
        return []
    with tempfile.TemporaryDirectory() as tmp:
        try:
            config = mkdocs_load_config(
                str(PROJECT_ROOT / "mkdocs.yml"), docs_dir=str(dest), site_dir=tmp
            )
            config = config.plugins.on_config(config)
            files = get_files(config)
            files = config.plugins.on_files(files, config=config)
            get_navigation(files, config)

            pages = []
            for file in files.documentation_pages():
                page = file.page
                page.read_source(config)
                pages.append({"title": page.title or file.url, "location": file.url})
        except Exception:
            return []
    return pages


def refresh_page_index(repo_id: str) -> None:
    PAGE_INDEX[repo_id] = build_page_index(repo_id)
    LAST_UPDATE[repo_id] = repo_last_update(repo_id)
    PAGE_INDEX_PATH.write_text(json.dumps(PAGE_INDEX, indent=2, ensure_ascii=False), encoding="utf-8")


def run_sync(repo_url: str, token: str | None, dest: Path) -> tuple[bool, str]:
    """Clona `dest` si no existe, o hace pull (fetch + fast-forward) si ya
    existe. Via pygit2/libgit2 en vez del `git` del sistema -- no depende
    de que el usuario tenga git instalado, y el token viaja por
    `RemoteCallbacks` en vez de incrustado en la URL del proceso, asi
    nunca queda visible en la lista de procesos (`ps`) de la maquina --
    limitacion que tenia la version anterior (subprocess), ver
    02 Motores de backend.md. Probado con clone/pull sin cambios/pull con
    fast-forward/pull con historias divergentes contra un repo bare
    local, y con el callback de credenciales contra un repo real."""
    callbacks = pygit2.RemoteCallbacks(credentials=pygit2.UserPass(token, "")) if token else None
    try:
        if not (dest / ".git").exists():
            pygit2.clone_repository(repo_url, str(dest), callbacks=callbacks)
            return True, "Sincronización correcta."

        repo = pygit2.Repository(str(dest))
        repo.remotes["origin"].fetch(callbacks=callbacks)

        branch_name = repo.head.shorthand
        remote_head = repo.references[f"refs/remotes/origin/{branch_name}"].target

        analysis, _ = repo.merge_analysis(remote_head)
        if analysis & pygit2.enums.MergeAnalysis.UP_TO_DATE:
            return True, "Sincronización correcta."
        if not (analysis & pygit2.enums.MergeAnalysis.FASTFORWARD):
            return False, "No se pudo sincronizar: hay cambios locales que ya no coinciden con el repositorio remoto."

        repo.checkout_tree(repo.get(remote_head))
        repo.lookup_reference(f"refs/heads/{branch_name}").set_target(remote_head)
        repo.head.set_target(remote_head)
        return True, "Sincronización correcta."
    except pygit2.GitError as e:
        return False, str(e) or "Error desconocido al sincronizar."


def mkdocs_is_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((MKDOCS_HOST, MKDOCS_PORT)) == 0


def kill_mkdocs() -> None:
    """Mata el mkdocs serve que lanzamos nosotros (via pidfile propio).

    Necesario porque `mkdocs serve --no-livereload` no activa ningun
    watcher de archivos (confirmado en el codigo fuente de
    mkdocs.commands.serve): un cambio de repo activo (nuevo
    `_runtime_mkdocs.yml`, ver `set_active_docs_dir()`) o un pull en
    caliente nunca se reflejan mientras el proceso siga vivo. La unica
    forma confiable de servir contenido nuevo es reiniciar el proceso,
    no reusar uno que ya estaba corriendo.
    """
    if not MKDOCS_PID_PATH.exists():
        return
    try:
        pid = int(MKDOCS_PID_PATH.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGTERM)
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
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
        [
            sys.executable, "-m", "mkdocs", "serve",
            "-f", str(RUNTIME_MKDOCS_CONFIG),
            "-a", f"{MKDOCS_HOST}:{MKDOCS_PORT}",
            "--no-livereload",
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    MKDOCS_PID_PATH.write_text(str(proc.pid), encoding="utf-8")
    for _ in range(60):
        if mkdocs_is_running():
            return
        time.sleep(0.1)


def ensure_mkdocs_running() -> None:
    """Arranca mkdocs solo si no hay nada escuchando en el puerto -- red de
    seguridad para las rutas de /wiki en caso de que el proceso muriera
    entre requests. No sirve para recoger contenido nuevo: `mkdocs serve
    --no-livereload` no activa ningun watcher de archivos (confirmado en
    el codigo fuente de mkdocs.commands.serve, el watch solo se registra
    si `livereload=True`), asi que un pull en caliente sobre el repo
    activo nunca se refleja mientras el proceso siga vivo. Para eso hace
    falta restart_mkdocs() (ver resync())."""
    if not mkdocs_is_running():
        spawn_mkdocs()


def restart_mkdocs() -> None:
    """Reinicia mkdocs siempre. Se usa cuando el repo activo cambia
    (conectar uno nuevo o seleccionar otro ya conectado), porque
    `_runtime_mkdocs.yml` paso a apuntar a un directorio distinto."""
    kill_mkdocs()
    spawn_mkdocs()


def ensure_serving(repo_id: str) -> None:
    """Asegura que mkdocs este sirviendo `repo_id` antes de proxiar una
    request de /wiki. Puede haber quedado sirviendo la documentacion de
    uso (ver docs()): SERVING_REPO_ID != repo_id detecta ese desfase y
    reencamina mkdocs solo, sin que el usuario tenga que volver a
    seleccionar su repo a mano. Si ya coincide, ensure_mkdocs_running()
    solo cubre el caso de que el proceso haya muerto entre requests."""
    global SERVING_REPO_ID
    if SERVING_REPO_ID != repo_id:
        set_active_docs_dir(repo_id)
        restart_mkdocs()
        SERVING_REPO_ID = repo_id
    else:
        ensure_mkdocs_running()


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
    global SERVING_REPO_ID
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

    set_active_docs_dir(repo_id)
    restart_mkdocs()
    SERVING_REPO_ID = repo_id
    refresh_page_index(repo_id)
    # A "/_admin", no a "/": el usuario necesita ver el resultado del
    # sync antes de que "/" empiece a servirle la wiki via proxy.
    return RedirectResponse("/_admin", status_code=303)


@app.post("/select/{repo_id}")
def select(repo_id: str):
    global SERVING_REPO_ID
    config = read_config()
    if get_repo(config, repo_id) is None:
        return RedirectResponse("/_admin", status_code=303)
    # Si el repo ya era el activo Y mkdocs ya lo esta sirviendo, reiniciar
    # solo hace esperar el rebuild completo (varios segundos) para
    # terminar sirviendo exactamente lo mismo. was_active ya no basta solo
    # (ver SERVING_REPO_ID): tras visitar /_docs, config["active_id"]
    # sigue apuntando aqui pero mkdocs esta sirviendo la documentacion de
    # uso -- en ese caso si hace falta reiniciar aunque "ya era el activo".
    already_serving = config["active_id"] == repo_id and SERVING_REPO_ID == repo_id
    config["active_id"] = repo_id
    write_config(config)
    set_active_docs_dir(repo_id)
    if already_serving:
        ensure_mkdocs_running()
    else:
        restart_mkdocs()
    SERVING_REPO_ID = repo_id
    return RedirectResponse("/wiki", status_code=303)


@app.post("/resync", response_class=HTMLResponse)
def resync(request: Request):
    global SERVING_REPO_ID
    config = read_config()
    active = get_active_repo(config)
    if active is None:
        return RedirectResponse("/_admin", status_code=303)
    token = keyring.get_password(KEYRING_SERVICE, token_key(active["id"]))
    ok, message = run_sync(active["repo_url"], token, REPOS_DIR / active["id"])
    if ok:
        # restart_mkdocs(), no ensure_mkdocs_running(): sin esto el pull
        # se traia a disco pero nunca se veia reflejado en la wiki servida
        # (ver docstring de ensure_mkdocs_running).
        set_active_docs_dir(active["id"])
        restart_mkdocs()
        SERVING_REPO_ID = active["id"]
        refresh_page_index(active["id"])
    return render_admin(request, config, sync_message=message, sync_ok=ok)


@app.post("/disconnect/{repo_id}")
def disconnect(repo_id: str):
    global SERVING_REPO_ID
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
    PAGE_INDEX_PATH.write_text(json.dumps(PAGE_INDEX, indent=2, ensure_ascii=False), encoding="utf-8")

    if was_active:
        set_active_docs_dir(None)
        kill_mkdocs()
        SERVING_REPO_ID = None

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
    try:
        upstream = await HTTPX_CLIENT.request(
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
                "last_update": relative_date(cached_last_update(r["id"])),
                "search": search_blob,
            }
        )
    return templates.TemplateResponse(
        request,
        "hub.html",
        {"repos": repos, "active_id": config["active_id"]},
    )


# La documentacion de uso se sincroniza a lo mucho una vez por corrida
# del proceso (ver docs()).
_docs_synced = False


@app.get("/_docs")
def docs():
    """Documentacion de uso de la app: la sincroniza y manda directo a
    /wiki. Sin DOCS_REPO_URL todavia, cae a /_admin -- no hay nada que
    mostrar. (No es "/docs": FastAPI ya usa esa ruta para su propio
    Swagger UI.)

    A proposito NUNCA se agrega a config["repos"]: es un recurso fijo de
    la app, mantenido por quien la desarrolla, no un repo que el usuario
    conecto. Si se guardara ahi igual que un repo normal, aparecia como
    tarjeta en el Hub (como si el usuario lo hubiera agregado) y como fila
    "Quitar"-able en el panel de Repositorios -- pudiendo incluso borrarlo
    por accidente. get_active_repo() sabe reconocerlo igual como activo
    sin que viva en esa lista.

    Por el mismo motivo, ver docs de uso NUNCA toca config["active_id"]:
    solo redirige mkdocs (en memoria, via SERVING_REPO_ID) a servir este
    repo. Antes si lo tocaba y lo persistia en disco -- el repo real del
    usuario aparecia como "desactivado" en Hub/Admin mientras leia la
    ayuda, y volver a el pagaba un rebuild completo de mas. ensure_serving()
    (usada por /wiki) detecta el desfase entre SERVING_REPO_ID y el repo
    activo de verdad y reencamina mkdocs sola, sin que el usuario tenga
    que volver a seleccionar su repo a mano."""
    global _docs_synced, SERVING_REPO_ID
    if not DOCS_REPO_URL:
        return RedirectResponse("/_admin", status_code=303)

    repo_id = repo_id_for(DOCS_REPO_URL)
    dest = REPOS_DIR / repo_id
    # El pull se hace UNA vez por corrida del proceso, no en cada visita:
    # antes cada clic en "Documentacion" pagaba un git pull de red completo
    # (varios segundos) aun teniendo el clon fresco en disco. La doc de uso
    # cambia con releases de la app, no minuto a minuto -- el mismo criterio
    # que el pull de arranque del lifespan para el repo activo. Sin clon
    # todavia (primera vez) si se bloquea: no hay nada que servir sin el.
    if not _docs_synced or not (dest / ".git").exists():
        run_sync(DOCS_REPO_URL, None, dest)
        _docs_synced = True
    if not (dest / ".git").exists():
        # Nunca se pudo clonar (ni antes ni ahora) -- no hay nada que servir.
        return RedirectResponse("/_admin", status_code=303)

    if SERVING_REPO_ID != repo_id:
        set_active_docs_dir(repo_id)
        restart_mkdocs()
        SERVING_REPO_ID = repo_id

    return RedirectResponse("/wiki", status_code=303)


@app.api_route("/wiki", methods=["GET", "POST", "HEAD"])
async def wiki_index(request: Request):
    active = get_active_repo(read_config())
    if active is None:
        return RedirectResponse("/", status_code=303)
    ensure_serving(active["id"])
    return await proxy_to_mkdocs(request, "wiki")


@app.api_route("/wiki/{path:path}", methods=["GET", "POST", "HEAD"])
async def wiki_proxy(request: Request, path: str):
    """Todo el contenido de la wiki (paginas, assets, search_index.json)
    vive bajo /wiki/ -- un prefijo fijo, no por repo, porque solo un
    repo esta activo a la vez (ver set_active_docs_dir). `mkdocs serve`
    ya sirve su contenido bajo ese mismo prefijo internamente (efecto de
    `site_url: http://.../wiki/` en mkdocs.yml), asi que se reenvia la
    ruta completa tal cual -- no hay que reescribir nada."""
    active = get_active_repo(read_config())
    if active is None:
        return RedirectResponse("/", status_code=303)
    ensure_serving(active["id"])
    return await proxy_to_mkdocs(request, f"wiki/{path}")
