#!/usr/bin/env python3
"""Punto de entrada unico de la app (prototipo de frontend).

FastAPI es el unico puerto que el usuario ve, con tres zonas:
- `/` -- el hub: que repos hay conectados, accesos directos.
- `/_admin` -- conectar, seleccionar y quitar repositorios.
- `/wiki/...` -- el contenido en si, servido directo desde el sitio
  estatico ya construido del repo activo -- ver `serve_site()`.

"Acerca de" no es una pantalla propia: es un modal (`about_modal.html`,
glassmorphism) que vive incrustado en el shell y en el header de la
wiki -- ver `openAboutModal()`.

Multi-repo (simple, a proposito): cada repo conectado vive clonado en
`repos/<repo_id>/`, y su sitio ya renderizado (Markdown a HTML, assets
del tema, `search_index.json`) vive en `sites/<repo_id>/` -- generado con
`mkdocs.commands.build.build()` (API de Python de MkDocs, sin subproceso)
cada vez que ese repo se sincroniza, ver `build_site()`. Solo uno esta
"activo" a la vez para el usuario, pero cambiar cual lo esta (`select()`)
no reconstruye nada ni reinicia ningun proceso: es servir archivos de una
carpeta de `sites/` en vez de otra. Sin symlink de por medio a proposito
(Windows no crea symlinks estilo Unix sin privilegios especiales): cada
repo tiene su propia carpeta de salida fija, no hay una ruta "activa"
compartida que reapuntar.

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
import tempfile
import unicodedata
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import keyring
import pygit2
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mkdocs.commands.build import build as mkdocs_build
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
# indice de paginas, clones de repos, sitios estaticos ya construidos).
# En un checkout de desarrollo o en el instalable de Windows (que vive en
# `$LOCALAPPDATA`, ya de por si escribible por el usuario), es el propio
# `PROJECT_ROOT` -- mismas rutas que siempre (config.json junto a
# server.py, repos/ junto al mkdocs.yml fuente). El paquete `.deb` de
# Linux en cambio instala el codigo bajo `/opt/marc`, propiedad de root --
# ahi PROJECT_ROOT no admite escritura, asi que todo el estado se mueve
# junto a `~/.local/share/marc` (XDG data dir), igual que cualquier otra
# app de escritorio en Linux separa binarios de estado de usuario.
if _project_root_writable():
    REPOS_DIR = PROJECT_ROOT / "repos"
    SITES_DIR = PROJECT_ROOT / "sites"
    CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
    PAGE_INDEX_PATH = Path(__file__).resolve().parent / "page_index.json"
else:
    _STATE_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "marc"
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    REPOS_DIR = _STATE_DIR / "repos"
    SITES_DIR = _STATE_DIR / "sites"
    CONFIG_PATH = _STATE_DIR / "config.json"
    PAGE_INDEX_PATH = _STATE_DIR / "page_index.json"

REPOS_DIR.mkdir(exist_ok=True)
SITES_DIR.mkdir(exist_ok=True)

KEYRING_SERVICE = "wiki-desktop-client"
LEGACY_TOKEN_KEY = "github-pat"  # esquema de un solo repo, pre-multi-repo

# Todo bajo /wiki/ y /_docs/ vive en la misma URL sin importar que repo esta
# activo -- select() puede cambiar que carpeta de SITES_DIR se sirve de una
# request a la siguiente. FileResponse/RedirectResponse no ponen Cache-Control
# por su cuenta, y el navegador cachea agresivamente sin el (en particular los
# 308 de forma practicamente permanente): sin este header, cambiar de repo
# activo no se nota en pestañas que ya habian visitado /wiki/ antes, sirviendo
# de cache el contenido del repo viejo -- confirmado con Playwright, viendo
# requestStart/responseStart en -1 (ninguna request de red real) en la segunda
# navegacion tras un select() a otro repo. Ver doc 28 en Obsidian.
NO_STORE_HEADERS = {"Cache-Control": "no-store"}

APP_VERSION = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()

# Repo fijo con la documentacion de uso de la app (no del contenido de
# un equipo) -- distinto de los repos que el usuario conecta. Vacio
# hasta que el repo exista; el boton del hub cae a /_admin mientras
# tanto. Cuando exista, poner aqui su URL publica de GitHub.
DOCS_REPO_URL: str | None = "https://github.com/Anton-Bazh/MARC-DOC.git"

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
        # Se construye el sitio estatico aunque el pull automatico falle
        # (ej. sin internet): sirve el contenido de la ultima sincronizacion
        # buena que haya en disco, en vez de dejar al usuario sin nada.
        if (dest / ".git").exists():
            build_site(active["id"])
            refresh_page_index(active["id"])

    # Los demas repos ya estan clonados en disco -- indexarlos y
    # construirlos no necesita red, solo si todavia no se habia hecho.
    for repo in config["repos"]:
        if repo["id"] not in PAGE_INDEX:
            refresh_page_index(repo["id"])
        ensure_site_built(repo["id"])
    yield


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


def build_site(repo_id: str) -> bool:
    """Build completo (Markdown a HTML, assets del tema, search_index.json)
    del repo a `SITES_DIR/<repo_id>/`, via la API de Python de MkDocs --
    mismo patron de override de `docs_dir`/`site_dir` que ya usaba
    `build_page_index()`, pero con `mkdocs.commands.build.build()` real en
    vez de solo leer metadata. `build()` corre `on_config` por su cuenta
    (no hace falta llamarlo aqui, a diferencia de `build_page_index()`).

    Reemplaza el viejo esquema de `mkdocs serve` + `_runtime_mkdocs.yml` +
    proxy HTTP: cada repo conectado queda con su sitio ya construido en
    disco tras cada sync (ver connect()/resync()/lifespan), asi que
    seleccionar uno u otro (`select()`) pasa a ser instantaneo -- servir
    archivos estaticos de una carpeta u otra, sin reconstruir nada ni
    reiniciar ningun proceso. Tambien elimina la necesidad del symlink
    `synced_docs/` de versiones viejas (Windows no crea symlinks estilo
    Unix sin privilegios especiales) sin sustituirlo por otro mecanismo
    fragil: aqui no hay ruta compartida "activa" que reapuntar, cada repo
    tiene su propia carpeta de salida fija."""
    dest = REPOS_DIR / repo_id
    if not (dest / ".git").exists():
        return False
    # El nombre del breadcrumb (config.extra["active_repo_name"], ver
    # hooks/expose_active_repo.py) debe ser el de ESTE repo, no el del
    # repo activo global -- distinto en dos casos reales: (1) aqui se
    # pre-construyen sitios de repos que todavia no son el activo (ver
    # lifespan/ensure_site_built), y (2) el repo fijo de documentacion de
    # uso (DOCS_REPO_URL) nunca es "activo" por diseno (ver docs()). Sin
    # esto, ambos casos heredaban el nombre del repo activo de
    # config.json, ajeno al contenido que en realidad se esta sirviendo.
    if DOCS_REPO_URL and repo_id == repo_id_for(DOCS_REPO_URL):
        repo_url = DOCS_REPO_URL
    else:
        repo = get_repo(read_config(), repo_id)
        repo_url = repo["repo_url"] if repo else None
    display_name = repo_display_name(repo_url) if repo_url else "wiki"
    try:
        config = mkdocs_load_config(
            str(PROJECT_ROOT / "mkdocs.yml"),
            docs_dir=str(dest),
            site_dir=str(SITES_DIR / repo_id),
            extra={"active_repo_name": display_name},
        )
        mkdocs_build(config)
    except Exception:
        return False
    return True


def ensure_site_built(repo_id: str) -> None:
    """Construye el sitio de `repo_id` solo si todavia no existe -- para
    los repos clonados que no son el activo (ver lifespan) o como red de
    seguridad si `serve_site()` encuentra la carpeta vacia (instalacion
    migrada desde antes de este esquema, o build anterior fallido)."""
    if not (SITES_DIR / repo_id / "index.html").exists():
        build_site(repo_id)


def _resolve_site_file(repo_id: str, path: str) -> tuple[Path, bool] | None:
    """Resuelve `path` dentro de `SITES_DIR/<repo_id>/`, sin permitir
    escapar ese directorio (path traversal via `..`). Devuelve
    `(archivo, es_index_de_directorio)` -- el segundo valor le dice a
    `serve_site()` si hace falta redirigir agregando la barra final antes
    de servir ese `index.html` (ver ahi el motivo)."""
    base = (SITES_DIR / repo_id).resolve()
    if not base.is_dir():
        return None
    candidate = (base / path).resolve()
    if candidate != base and base not in candidate.parents:
        return None
    if candidate.is_dir():
        return candidate / "index.html", True
    return candidate, False


def serve_site(repo_id: str, path: str, url_prefix: str):
    """Sirve `path` desde el sitio ya construido de `repo_id`, bajo el
    prefijo de URL `url_prefix` (`wiki` o `_docs`). Reemplaza
    `proxy_to_mkdocs()`: ya no hay proceso vivo al que reenviar la
    request, `FileResponse` lee el archivo directo de disco (con soporte
    nativo de ETag/Range/tipo MIME por extension).

    Si `path` (no vacio) cae en un directorio pero la URL no traia la
    barra final (ej. `/wiki/00-indice` en vez de `/wiki/00-indice/`),
    redirige agregandola en vez de servir el `index.html` directo en esa
    URL -- los links relativos de la pagina (assets, otras paginas) se
    resuelven contra la URL actual del navegador, y sin la barra final
    resuelven un nivel arriba de lo que deberian (`/wiki/00-indice` +
    `assets/x.css` -> `/wiki/assets/x.css` en vez de
    `/wiki/00-indice/assets/x.css`). Los servidores estaticos normales
    (Apache, Nginx, y el `mkdocs serve` que este esquema reemplaza) hacen
    esta misma redirección al servir un directorio; sin ella, la wiki
    cargaba pero cada asset/link relativo apuntaba mal en cuanto se
    entraba a una pagina sin escribir la barra final a mano (confirmado
    navegando con Playwright, no se veia con `curl` porque curl no sigue
    el `<meta refresh>` ni resuelve URLs relativas). `path == ""`
    (la raiz, `/wiki/` o `/_docs/`) nunca redirige aqui -- quien registra
    la ruta bare sin barra (`wiki_index()`) ya redirige por su cuenta
    antes de llegar a esta funcion; tratarla igual aqui causaria un loop
    de redirects (`path=""` no distingue por si solo si la URL original
    ya traia o no la barra)."""
    ensure_site_built(repo_id)
    resolved = _resolve_site_file(repo_id, path)
    if resolved is None:
        return HTMLResponse("<p>Página no encontrada.</p>", status_code=404, headers=NO_STORE_HEADERS)
    file_path, is_dir_index = resolved
    if is_dir_index and path != "" and not path.endswith("/"):
        target = f"/{url_prefix}/{path}".rstrip("/") + "/"
        return RedirectResponse(target, status_code=308, headers=NO_STORE_HEADERS)
    if not file_path.is_file():
        not_found = SITES_DIR / repo_id / "404.html"
        if not_found.is_file():
            return FileResponse(not_found, status_code=404, headers=NO_STORE_HEADERS)
        return HTMLResponse("<p>Página no encontrada.</p>", status_code=404, headers=NO_STORE_HEADERS)
    return FileResponse(file_path, headers=NO_STORE_HEADERS)


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

    build_site(repo_id)
    refresh_page_index(repo_id)
    # A "/_admin", no a "/": el usuario necesita ver el resultado del
    # sync antes de que "/" empiece a servirle la wiki.
    return RedirectResponse("/_admin", status_code=303)


@app.post("/select/{repo_id}")
def select(repo_id: str):
    config = read_config()
    if get_repo(config, repo_id) is None:
        return RedirectResponse("/_admin", status_code=303)
    # Sin restart ni rebuild que esperar: el sitio de este repo ya quedo
    # construido en su ultimo sync (ver connect()/resync()/lifespan), asi
    # que cambiar de repo activo es solo actualizar que carpeta de
    # SITES_DIR sirve /wiki en la siguiente request (ver serve_site()).
    config["active_id"] = repo_id
    write_config(config)
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
        build_site(active["id"])
        refresh_page_index(active["id"])
    return render_admin(request, config, sync_message=message, sync_ok=ok)


@app.post("/disconnect/{repo_id}")
def disconnect(repo_id: str):
    config = read_config()
    if get_repo(config, repo_id) is None:
        return RedirectResponse("/_admin", status_code=303)

    config["repos"] = [r for r in config["repos"] if r["id"] != repo_id]
    if config["active_id"] == repo_id:
        config["active_id"] = None
    write_config(config)

    try:
        keyring.delete_password(KEYRING_SERVICE, token_key(repo_id))
    except keyring.errors.PasswordDeleteError:
        pass
    # Se borra para que una futura reconexion a esta URL parta de un
    # `clone` limpio -- un `pull` contra un remoto distinto fallaria
    # (historias no relacionadas). El sitio construido se borra junto con
    # el clon -- sin el clon no hay como reconstruirlo si algo lo pidiera.
    shutil.rmtree(REPOS_DIR / repo_id, ignore_errors=True)
    shutil.rmtree(SITES_DIR / repo_id, ignore_errors=True)

    PAGE_INDEX.pop(repo_id, None)
    PAGE_INDEX_PATH.write_text(json.dumps(PAGE_INDEX, indent=2, ensure_ascii=False), encoding="utf-8")

    return RedirectResponse("/_admin", status_code=303)


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
    """Documentacion de uso de la app: la sincroniza y manda a /_docs/,
    su propio sitio estatico independiente del repo activo del usuario
    (ver docs_content()). Sin DOCS_REPO_URL todavia, cae a /_admin -- no
    hay nada que mostrar. (No es "/docs": FastAPI ya usa esa ruta para su
    propio Swagger UI.)

    A proposito NUNCA se agrega a config["repos"]: es un recurso fijo de
    la app, mantenido por quien la desarrolla, no un repo que el usuario
    conecto. Si se guardara ahi igual que un repo normal, aparecia como
    tarjeta en el Hub (como si el usuario lo hubiera agregado) y como fila
    "Quitar"-able en el panel de Repositorios -- pudiendo incluso borrarlo
    por accidente. get_active_repo() sabe reconocerlo igual como activo
    sin que viva en esa lista (queda como red de seguridad para instalaciones
    que ya tuvieran ese estado guardado desde antes de este esquema).

    Por el mismo motivo, ver la doc de uso NUNCA toca config["active_id"]:
    tiene su propio sitio construido y su propio prefijo de URL (/_docs/),
    asi que no hace falta tocar que repo esta "activo" para el usuario --
    a diferencia del viejo esquema de un solo `mkdocs serve` compartido,
    donde mostrar la doc de uso exigia repuntar el unico proceso vivo."""
    global _docs_synced
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

    build_site(repo_id)
    return RedirectResponse("/_docs/", status_code=303)


@app.get("/_docs/{path:path}")
async def docs_content(path: str):
    """Sirve el sitio ya construido de la documentacion de uso, con su
    propio prefijo -- independiente de cual repo tenga activo el usuario
    (ver docs())."""
    if not DOCS_REPO_URL:
        return RedirectResponse("/_admin", status_code=303)
    return serve_site(repo_id_for(DOCS_REPO_URL), path, "_docs")


@app.get("/wiki")
def wiki_index():
    # Redirige a /wiki/ (con barra) siempre: sin ella, los links
    # relativos de la primera pagina (assets, otras paginas) resuelven un
    # nivel arriba de lo que deberian -- ver el docstring de serve_site().
    # Si no hay repo activo, / mismo lo explica mejor que un 404 aqui.
    if get_active_repo(read_config()) is None:
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/wiki/", status_code=308, headers=NO_STORE_HEADERS)


@app.get("/wiki/{path:path}")
async def wiki_proxy(path: str):
    """Todo el contenido de la wiki (paginas, assets, search_index.json)
    vive bajo /wiki/ -- un prefijo fijo, no por repo, porque solo un repo
    del usuario esta activo a la vez. Sirve directo del sitio ya
    construido de ese repo en SITES_DIR (ver serve_site())."""
    active = get_active_repo(read_config())
    if active is None:
        return RedirectResponse("/", status_code=303)
    return serve_site(active["id"], path, "wiki")
