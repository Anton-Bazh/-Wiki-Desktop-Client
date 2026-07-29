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

import hashlib
import json
import os
import re
import shutil
import signal
import tempfile
import threading
import time
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

# Ultima huella conocida (ver _dir_fingerprint) de cada repo local,
# poblada junto con PAGE_INDEX (ver refresh_page_index) -- sync_check()
# la compara contra la huella actual para saber si la carpeta cambio en
# disco desde la ultima vez que se construyo, sin necesitar git.
LOCAL_FINGERPRINT: dict[str, str] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Al iniciar el proceso (lo mas parecido a "abrir la app" en este
    prototipo web), si ya hay un repo activo de una sesion anterior, se
    hace el pull automatico -- tal como lo pide la spec original, en vez
    de depender de que el usuario le de clic a Resincronizar."""
    global startup_sync
    config = read_config()
    active = get_active_repo(config)
    if active is not None and "local_path" not in active:
        token = keyring.get_password(KEYRING_SERVICE, token_key(active["id"]))
        dest = repo_dir(active["id"])
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
        if "local_path" in repo:
            # Una carpeta local puede haber cambiado mientras MARC no
            # corria (el usuario la edita con sus propias herramientas,
            # fuera del control de MARC) -- a diferencia de un repo de
            # GitHub, que ya refleja fielmente el ultimo pull, aqui
            # conviene reconstruir siempre al arrancar, no solo si falta
            # el sitio (ensure_site_built() de abajo).
            if repo_dir(repo["id"]).is_dir():
                build_site(repo["id"])
                refresh_page_index(repo["id"])
            continue
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


def repo_dir(repo_id: str) -> Path:
    """Carpeta donde vive el contenido de `repo_id`: para un repo local
    (`local_path`, ver connect_local()) es la carpeta del usuario tal
    cual -- MARC nunca clona ahi, la lee directo. Para un repo de GitHub
    es la ruta que el usuario haya elegido al conectar (`custom_path`, ver
    connect()) o la interna de REPOS_DIR si no eligio ninguna. Repos que
    no viven en config["repos"] (ej. DOCS_REPO_URL, ver docs()) siempre
    caen al default -- nunca tienen custom_path/local_path por diseno,
    asi que no hace falta distinguirlos aqui."""
    repo = get_repo(read_config(), repo_id)
    if repo and "local_path" in repo:
        return Path(repo["local_path"])
    custom = repo.get("custom_path") if repo else None
    return Path(custom) if custom else REPOS_DIR / repo_id


def _repo_is_local(repo: dict | None) -> bool:
    return bool(repo and "local_path" in repo)


def _dest_ready(dest: Path, is_local: bool) -> bool:
    """Si `dest` ya tiene contenido para construir: una carpeta local
    siempre esta lista (es la carpeta real del usuario, MARC nunca la
    clona) -- un repo de GitHub necesita que `run_sync()` ya haya
    corrido (`.git` presente, la señal de que el clone se completo)."""
    return dest.is_dir() if is_local else (dest / ".git").exists()


def _local_repo_id(path: Path) -> str:
    """id deterministico para una carpeta local: mismo criterio que
    repo_id_for() con una URL de GitHub -- la MISMA carpeta produce
    siempre el mismo id (reconectarla reactiva la entrada existente en
    vez de duplicarla), y el hash evita colision entre carpetas de
    nombre igual mismo en rutas distintas (ej. dos carpetas 'notas')."""
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:8]
    return f"{_slugify(path.name)}-{digest}"


def _path_collision(config: dict, repo_id: str, path: Path) -> bool:
    """Si `path` ya es el destino en disco de OTRO repo conectado (local,
    o de GitHub con/sin custom_path) -- compartir carpeta entre dos repos
    los dejaria pisandose el contenido uno al otro."""
    return any(
        r["id"] != repo_id
        and Path(r.get("local_path") or r.get("custom_path") or (REPOS_DIR / r["id"])) == path
        for r in config["repos"]
    )


def _validate_external_path(path: Path) -> str | None:
    """None si `path` es valida como carpeta gestionada por el usuario
    (fuera de REPOS_DIR/SITES_DIR) -- si no, el mensaje de error a
    mostrar. Regla compartida por connect() (custom_path) y
    connect_local() (local_path): ninguna puede vivir dentro de la
    carpeta interna de MARC, se pisaria con lo que MARC ya gestiona ahi."""
    if path == REPOS_DIR or REPOS_DIR in path.parents or path == SITES_DIR or SITES_DIR in path.parents:
        return "Elige una carpeta fuera de la carpeta interna de MARC."
    return None


def _dir_fingerprint(dest: Path) -> str:
    """Huella barata del estado de una carpeta local: cuenta de archivos
    + el mtime mas reciente entre todos (carpetas/archivos ocultos
    aparte, mismo criterio que browse_fs()) -- para que sync_check() sepa
    si hace falta reconstruir sin tener que leer/comparar el contenido
    entero. No es criptografica, solo necesita cambiar cuando algo real
    dentro de la carpeta cambia."""
    latest = 0.0
    count = 0
    for root, dirnames, filenames in os.walk(dest):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            try:
                mtime = (Path(root) / name).stat().st_mtime
            except OSError:
                continue
            count += 1
            latest = max(latest, mtime)
    return f"{count}:{latest}"


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
    dest = repo_dir(repo_id)
    repo = get_repo(read_config(), repo_id)
    is_local = _repo_is_local(repo)
    if not _dest_ready(dest, is_local):
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
        display_name = repo_display_name(DOCS_REPO_URL)
    elif is_local:
        display_name = Path(repo["local_path"]).name
    else:
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
    dest = repo_dir(repo_id)
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
    dest = repo_dir(repo_id)
    if not _dest_ready(dest, _repo_is_local(get_repo(read_config(), repo_id))):
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
    repo = get_repo(read_config(), repo_id)
    if _repo_is_local(repo):
        LOCAL_FINGERPRINT[repo_id] = _dir_fingerprint(repo_dir(repo_id))
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


def render_hub(
    request: Request,
    config: dict,
    *,
    admin_open: bool = False,
    local_tab: bool = False,
    error: str | None = None,
    repo_url_prefill: str = "",
    custom_path_prefill: str = "",
    local_path_prefill: str = "",
    editing_id: str | None = None,
    sync_message: str | None = None,
    sync_ok: bool | None = None,
):
    """Renderiza el Hub -- el panel de "Repositorios" ya no es una pagina
    aparte (ver admin_modal.html, incluido una sola vez en shell.html):
    viaja siempre incrustado como modal en esta misma respuesta, cerrado
    por defecto. `admin_open` es lo unico que decide si se abre solo al
    cargar la pagina (ver DOMContentLoaded en admin_modal.html) -- true
    cuando se llega desde /_admin, o justo despues de conectar/editar/
    desconectar/resincronizar un repo, para que el usuario vea el
    resultado sin tener que reabrirlo el mismo. Una sola lista de repos
    alimenta tanto las tarjetas del hub (badge/page_count/last_update/
    search) como las filas del modal (owner/path)."""
    repos = []
    for r in config["repos"]:
        is_local = _repo_is_local(r)
        name = Path(r["local_path"]).name if is_local else repo_display_name(r["repo_url"])
        pages = PAGE_INDEX.get(r["id"], [])
        search_blob = " ".join([name] + [p["title"] for p in pages]).lower()
        repos.append(
            {
                **r,
                "name": name,
                "is_local": is_local,
                "owner": "" if is_local else repo_owner(r["repo_url"]),
                "path": r["local_path"] if is_local else (r.get("custom_path") or str(REPOS_DIR / r["id"])),
                "badge": badge_class(r["id"]),
                "page_count": len(pages),
                "last_update": relative_date(cached_last_update(r["id"])),
                "search": search_blob,
            }
        )
    return templates.TemplateResponse(
        request,
        "hub.html",
        {
            "repos": repos,
            "active_id": config["active_id"],
            "admin_open": admin_open,
            "local_tab": local_tab,
            "error": error,
            "repo_url": repo_url_prefill,
            "custom_path": custom_path_prefill,
            "local_path": local_path_prefill,
            # Placeholder del campo de carpeta custom/local: una ruta real
            # de este sistema (no un texto generico), para que se lea de
            # un vistazo como una ruta de archivos y no como un campo
            # vacio cualquiera (ver admin_modal.html).
            "custom_path_example": str(REPOS_DIR / "mi-repo"),
            "editing_id": editing_id,
            "sync_message": sync_message,
            "sync_ok": sync_ok,
        },
    )


@app.get("/_admin", response_class=HTMLResponse)
def admin(request: Request, edit: str | None = None):
    """Ya no es una pagina propia: redirige el mismo Hub, con el modal de
    Repositorios abierto de entrada (`admin_open=True`). Sigue existiendo
    como ruta real (no un simple alias del boton del Hub) porque el header
    de la wiki (`theme_overrides/partials/header.html`) vive en un sitio
    estatico aparte, sin el modal incrustado -- ese link sigue navegando
    aqui de verdad, y aterriza en el Hub con el panel ya abierto."""
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
    # ?edit=<repo_id> (boton "Editar" de cada fila, ver admin_modal.html)
    # precarga el formulario de conectar con los datos de ese repo --
    # "editar" no es una operacion nueva, es resubmitir /connect con la
    # URL que ya tenia (mismo repo_id) y la carpeta que quiera cambiar;
    # connect() ya sabe tratar eso como reconexion en vez de alta nueva.
    edit_repo = get_repo(config, edit) if edit else None
    if edit_repo is not None and _repo_is_local(edit_repo):
        # Los repos locales no tienen edicion propia (ver connect_local():
        # reconectar la misma carpeta ya reactiva la entrada existente en
        # vez de duplicarla) -- un ?edit= manual a uno de estos se
        # ignora en vez de intentar precargar un repo_url que no existe.
        edit_repo = None
    return render_hub(
        request,
        config,
        admin_open=True,
        sync_message=sync_message,
        sync_ok=sync_ok,
        repo_url_prefill=edit_repo["repo_url"] if edit_repo else "",
        custom_path_prefill=(edit_repo.get("custom_path", "") if edit_repo else ""),
        editing_id=edit_repo["id"] if edit_repo else None,
    )


@app.post("/connect", response_class=HTMLResponse)
def connect(request: Request, repo_url: str = Form(...), token: str = Form(""), custom_path: str = Form("")):
    repo_url = repo_url.strip()
    token = token.strip() or None
    custom_path = custom_path.strip() or None
    config = read_config()

    if not repo_url.startswith("https://github.com/"):
        return render_hub(
            request,
            config,
            admin_open=True,
            error="Solo se soportan repositorios de GitHub (https://github.com/...) en esta versión.",
            repo_url_prefill=repo_url,
            custom_path_prefill=custom_path or "",
        )

    repo_id = repo_id_for(repo_url)
    existing = get_repo(config, repo_id)
    # Ruta donde vivia el clon ANTES de este submit (si el repo ya estaba
    # conectado) -- para poder limpiar la carpeta vieja despues si el
    # usuario esta editando y cambio de carpeta (ver mas abajo). Nunca se
    # calcula con repo_dir() aqui: ya tenemos `existing` en mano, y
    # ademas el disco todavia no se toco, asi que es exactamente la ruta
    # real de donde viene el repo.
    old_dest = None
    old_was_custom = False
    if existing is not None:
        old_was_custom = bool(existing.get("custom_path"))
        old_dest = Path(existing["custom_path"]) if old_was_custom else REPOS_DIR / repo_id

    if custom_path:
        dest = Path(custom_path).expanduser()
        if not dest.is_absolute():
            return render_hub(
                request, config, admin_open=True,
                error="La carpeta debe ser una ruta absoluta (ej. /home/tu-usuario/mis-wikis/repo).",
                repo_url_prefill=repo_url, custom_path_prefill=custom_path,
            )
        path_error = _validate_external_path(dest)
        if path_error:
            return render_hub(
                request, config, admin_open=True, error=path_error,
                repo_url_prefill=repo_url, custom_path_prefill=custom_path,
            )
        # Colision: otro repo conectado ya usa esa misma carpeta como
        # destino (custom, local, o default) -- clonar ahi tambien lo
        # dejaria con dos repos distintos peleando por el mismo working
        # tree.
        if _path_collision(config, repo_id, dest):
            return render_hub(
                request, config, admin_open=True,
                error="Esa carpeta ya la está usando otro repositorio conectado.",
                repo_url_prefill=repo_url, custom_path_prefill=custom_path,
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        dest = REPOS_DIR / repo_id

    ok, message = run_sync(repo_url, token, dest)
    if not ok:
        return render_hub(
            request, config, admin_open=True, error=message,
            repo_url_prefill=repo_url, custom_path_prefill=custom_path or "",
        )

    if token:
        keyring.set_password(KEYRING_SERVICE, token_key(repo_id), token)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, token_key(repo_id))
        except keyring.errors.PasswordDeleteError:
            pass

    if existing is None:
        entry = {"id": repo_id, "repo_url": repo_url}
        if custom_path:
            entry["custom_path"] = str(dest)
        config["repos"].append(entry)
    else:
        if custom_path:
            existing["custom_path"] = str(dest)
        else:
            existing.pop("custom_path", None)
    config["active_id"] = repo_id
    write_config(config)

    # Si esto era una edicion y cambio de carpeta, el clon viejo queda
    # huerfano -- se borra solo si MARC lo gestionaba (nunca si el usuario
    # ya habia elegido esa carpeta vieja, la misma regla que disconnect()).
    if existing is not None and old_dest is not None and old_dest != dest and not old_was_custom:
        shutil.rmtree(old_dest, ignore_errors=True)

    build_site(repo_id)
    refresh_page_index(repo_id)
    # A "/_admin", no a "/": el usuario necesita ver el resultado del
    # sync antes de que "/" empiece a servirle la wiki.
    return RedirectResponse("/_admin", status_code=303)


@app.post("/connect-local", response_class=HTMLResponse)
def connect_local(request: Request, local_path: str = Form(...)):
    """Conecta una carpeta del propio equipo como fuente de una wiki, sin
    pedir ninguna URL de GitHub -- MARC la lee directo (nunca la clona,
    nunca hace fetch/push/pull sobre ella), tenga o no su propio `.git`
    interno (si lo tiene, solo se aprovecha para leer la fecha del ultimo
    commit en el Hub, ver repo_last_update(); MARC jamas toca ese git).
    El id sale de la ruta misma (_local_repo_id()), asi que reconectar la
    misma carpeta reactiva la entrada que ya existia en vez de duplicarla
    -- no hace falta una operacion de "editar" aparte para esta fuente."""
    local_path = local_path.strip()
    config = read_config()

    path = Path(local_path).expanduser()
    if not path.is_absolute():
        return render_hub(
            request, config, admin_open=True, local_tab=True,
            error="La carpeta debe ser una ruta absoluta (ej. /home/tu-usuario/mi-wiki).",
            local_path_prefill=local_path,
        )
    path = path.resolve()
    if not path.is_dir():
        return render_hub(
            request, config, admin_open=True, local_tab=True,
            error="Esa carpeta no existe.",
            local_path_prefill=local_path,
        )
    path_error = _validate_external_path(path)
    if path_error:
        return render_hub(
            request, config, admin_open=True, local_tab=True, error=path_error,
            local_path_prefill=local_path,
        )

    repo_id = _local_repo_id(path)
    if get_repo(config, repo_id) is None:
        if _path_collision(config, repo_id, path):
            return render_hub(
                request, config, admin_open=True, local_tab=True,
                error="Esa carpeta ya la está usando otro repositorio conectado.",
                local_path_prefill=local_path,
            )
        config["repos"].append({"id": repo_id, "local_path": str(path)})
    config["active_id"] = repo_id
    write_config(config)

    build_site(repo_id)
    refresh_page_index(repo_id)
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
    if "local_path" in active:
        # Sin git de por medio que sincronizar -- "Resincronizar" en una
        # carpeta local solo fuerza una relectura inmediata en vez de
        # esperar al poll de /api/sync-check (ver ahi el mismo criterio
        # de huella por mtime).
        ok = build_site(active["id"])
        message = "Actualizado desde la carpeta local." if ok else "No se pudo leer esa carpeta -- ¿sigue existiendo?"
        if ok:
            refresh_page_index(active["id"])
        return render_hub(request, config, admin_open=True, sync_message=message, sync_ok=ok)
    token = keyring.get_password(KEYRING_SERVICE, token_key(active["id"]))
    ok, message = run_sync(active["repo_url"], token, repo_dir(active["id"]))
    if ok:
        build_site(active["id"])
        refresh_page_index(active["id"])
    return render_hub(request, config, admin_open=True, sync_message=message, sync_ok=ok)


def _head_oid(dest: Path) -> str | None:
    """OID del HEAD local, o None sin clon todavia -- para detectar si
    run_sync() de verdad trajo commits nuevos (ver sync_check())."""
    if not (dest / ".git").exists():
        return None
    try:
        return str(pygit2.Repository(str(dest)).head.target)
    except pygit2.GitError:
        return None


@app.get("/api/browse-fs")
def browse_fs(path: str = ""):
    """Explorador de carpetas propio (HTML/CSS/JS, ver el modal anidado
    en admin_modal.html) para elegir la carpeta custom de un repo.
    Reemplaza un primer intento con el selector nativo de tkinter: se veia
    anticuado en Linux (Tk no hereda el tema GTK del sistema, ni con el
    Tcl/Tk que ya trae empacado el runtime) y arreglarlo de verdad hubiera
    significado agregar zenity/kdialog como dependencia del sistema --
    justo lo que esta arquitectura evita a proposito (ver pygit2/vendor/
    Python autocontenido en el docstring de arriba). Este explorador en
    cambio es HTML plano: se ve identico (y ya combina con el resto de la
    UI) en Linux y Windows, sin depender de nada del sistema operativo.

    Solo devuelve directorios (nunca archivos, no hace falta para elegir
    carpeta) y se salta los ocultos (empiezan con '.') para no saturar la
    lista -- no hay forma de mostrarlos desde la UI, decision consciente
    para mantener esto simple. Sin `path` (primera apertura) arranca en el
    home del usuario."""
    base = Path(path).expanduser() if path else Path.home()
    if not base.is_dir():
        base = Path.home()
    base = base.resolve()

    try:
        entries = sorted(
            (p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name.lower(),
        )
        dirs = [{"name": p.name, "path": str(p)} for p in entries]
        error = None
    except PermissionError:
        dirs = []
        error = "Sin permiso para ver el contenido de esta carpeta."

    parent = base.parent
    return {
        "path": str(base),
        # None en la raiz del filesystem (`/` en Linux, `C:\` en Windows):
        # ahi `parent` coincide con el propio directorio, nada mas arriba
        # que subir. El JS usa esto para ocultar la fila ".." en ese caso.
        "parent": str(parent) if parent != base else None,
        "dirs": dirs,
        "error": error,
    }


@app.post("/api/sync-check")
def sync_check():
    """Poll silencioso desde el JS de cada pagina de /wiki/ (ver
    theme_overrides/main.html): hace el mismo fetch+ff-merge que
    Resincronizar pero sin pasar por /_admin, y solo reconstruye el sitio
    si el HEAD realmente cambio -- asi el poll periodico no reconstruye
    de a gratis cuando el repo ya estaba al dia."""
    config = read_config()
    active = get_active_repo(config)
    if active is None:
        return {"ok": True, "updated": False}
    dest = repo_dir(active["id"])
    if "local_path" in active:
        # Nada que hacer fetch/pull aqui -- el usuario edita esta carpeta
        # con sus propias herramientas, fuera del control de MARC. La
        # huella (ver _dir_fingerprint) cambia si algo real cambio desde
        # la ultima vez que se construyo (refresh_page_index() la deja al
        # dia cada vez), asi el poll solo reconstruye cuando hace falta.
        if not dest.is_dir():
            return {"ok": False, "updated": False}
        updated = _dir_fingerprint(dest) != LOCAL_FINGERPRINT.get(active["id"])
        if updated:
            build_site(active["id"])
            refresh_page_index(active["id"])
        return {"ok": True, "updated": updated}
    before = _head_oid(dest)
    token = keyring.get_password(KEYRING_SERVICE, token_key(active["id"]))
    ok, _ = run_sync(active["repo_url"], token, dest)
    updated = ok and _head_oid(dest) != before
    if updated:
        build_site(active["id"])
        refresh_page_index(active["id"])
    return {"ok": ok, "updated": updated}


@app.post("/shutdown")
def shutdown():
    """Boton 'Salir' del shell/wiki: termina el proceso del backend --
    unica otra forma de matarlo ademas de cerrar la ventana que lo
    arranco (ver installer/*/launcher.py, `open_window()` solo mata el
    backend si el sigue vivo y fue el mismo lanzador quien lo arranco).
    SIGTERM en un hilo aparte, con un respiro breve, para que uvicorn
    alcance a mandar esta respuesta 200 antes de que el shutdown
    interrumpa la conexion."""
    def _stop():
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_stop, daemon=True).start()
    return {"ok": True}


@app.post("/disconnect/{repo_id}")
def disconnect(repo_id: str):
    config = read_config()
    repo = get_repo(config, repo_id)
    if repo is None:
        return RedirectResponse("/_admin", status_code=303)

    is_local = _repo_is_local(repo)
    is_custom = bool(repo.get("custom_path"))
    if is_local:
        dest = Path(repo["local_path"])
    else:
        dest = Path(repo["custom_path"]) if is_custom else REPOS_DIR / repo_id

    config["repos"] = [r for r in config["repos"] if r["id"] != repo_id]
    if config["active_id"] == repo_id:
        config["active_id"] = None
    write_config(config)

    try:
        keyring.delete_password(KEYRING_SERVICE, token_key(repo_id))
    except keyring.errors.PasswordDeleteError:
        pass
    # El sitio construido siempre se borra (lo gestiona MARC, sin el clon
    # no hay como reconstruirlo si algo lo pidiera). El CLON en si solo se
    # borra si vivia en la carpeta interna de MARC -- para que una futura
    # reconexion a esta URL parta de un `clone` limpio (un `pull` contra un
    # remoto distinto fallaria, historias no relacionadas). Si el usuario
    # eligio la carpeta el mismo (`custom_path`) o conecto una carpeta
    # local (`local_path`), nunca se borra al desconectar: es su carpeta,
    # el decide si borrarla o seguir usandola con otra herramienta (ver
    # connect()/connect_local()).
    if not is_custom and not is_local:
        shutil.rmtree(dest, ignore_errors=True)
    shutil.rmtree(SITES_DIR / repo_id, ignore_errors=True)

    PAGE_INDEX.pop(repo_id, None)
    LOCAL_FINGERPRINT.pop(repo_id, None)
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
    la mencione, porque alguna de sus paginas si). El modal de
    Repositorios viaja incrustado (ver render_hub()) pero cerrado."""
    return render_hub(request, read_config())


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
