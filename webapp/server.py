#!/usr/bin/env python3
"""Pantalla de conexion al repositorio (prototipo de frontend).

Sirve un formulario donde el usuario pega la URL del repo de GitHub y,
si es privado, un Personal Access Token. El token se guarda en el
keychain nativo del SO (via `keyring`, backend SecretService en Linux/
gnome-keyring) -- nunca en el archivo de configuracion ni en disco en
texto plano. Dispara el clone/pull con esas credenciales y, si sale
bien, deja lista la carpeta `synced_docs/` que sirve `mkdocs serve`.

Prototipo: usa el `git` del sistema via subprocess, con el token
embebido en la URL de clone solo para el proceso hijo. La version final
(Tauri + git2-rs) pasara las credenciales por `RemoteCallbacks` en vez
de la URL, para no dejarlas visibles en la lista de procesos (`ps`) de
la maquina -- limitacion conocida de este prototipo, ver 02 Motores de
backend.md.
"""

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import keyring
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYNCED_DOCS = PROJECT_ROOT / "synced_docs"
CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
MKDOCS_PID_PATH = Path(__file__).resolve().parent / "mkdocs.pid"
MKDOCS_HOST, MKDOCS_PORT = "127.0.0.1", 8765

KEYRING_SERVICE = "wiki-desktop-client"
KEYRING_KEY = "github-pat"

# Resultado del sync automatico de arranque (None si no habia repo
# configurado todavia). Se muestra una vez en la pantalla de conectado
# para que el usuario sepa que se refresco solo, sin tener que adivinar.
startup_sync: tuple[bool, str] | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Al iniciar el proceso (lo mas parecido a "abrir la app" en este
    prototipo web), si ya hay un repo configurado de una sesion anterior,
    se hace el pull automatico -- tal como lo pide la spec original,
    en vez de depender de que el usuario le de clic a Resincronizar."""
    global startup_sync
    config = read_config()
    if config is not None:
        token = keyring.get_password(KEYRING_SERVICE, KEYRING_KEY)
        ok, message = run_sync(config["repo_url"], token)
        startup_sync = (ok, message)
        # Se levanta la wiki aunque el pull automatico falle (ej. sin
        # internet): sirve el contenido de la ultima sincronizacion buena
        # que haya en disco, en vez de dejar al usuario sin nada.
        if (SYNCED_DOCS / ".git").exists():
            ensure_mkdocs_running()
    yield


app = FastAPI(title="Wiki Desktop Client — Conexión", lifespan=lifespan)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))


def read_config() -> dict | None:
    if not CONFIG_PATH.exists():
        return None
    import json

    return json.loads(CONFIG_PATH.read_text())


def write_config(repo_url: str) -> None:
    import json

    CONFIG_PATH.write_text(json.dumps({"repo_url": repo_url}, indent=2))


def clear_config() -> None:
    CONFIG_PATH.unlink(missing_ok=True)


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


def run_sync(repo_url: str, token: str | None) -> tuple[bool, str]:
    """Clona synced_docs/ si no existe, o hace pull si ya existe."""
    clone_url = authenticated_url(repo_url, token)
    try:
        if not (SYNCED_DOCS / ".git").exists():
            result = subprocess.run(
                ["git", "clone", clone_url, str(SYNCED_DOCS)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        else:
            # Si cambio el token o la URL, se actualiza el remoto antes de tirar de el.
            subprocess.run(
                ["git", "remote", "set-url", "origin", clone_url],
                cwd=SYNCED_DOCS,
                capture_output=True,
                text=True,
                timeout=15,
            )
            result = subprocess.run(
                ["git", "pull", "--ff-only", "origin"],
                cwd=SYNCED_DOCS,
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
    que `synced_docs/` fue borrado y re-clonado (nuevo inodo, mismo path):
    se queda sirviendo el build viejo indefinidamente. La unica forma
    confiable de servir el contenido nuevo tras un `clone` es reiniciar
    el proceso, no reusar uno que ya estaba corriendo.
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
    proc = subprocess.Popen(
        [sys.executable, "-m", "mkdocs", "serve", "-a", f"{MKDOCS_HOST}:{MKDOCS_PORT}"],
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
    tras un resync (pull en caliente), donde el watcher si funciona."""
    if not mkdocs_is_running():
        spawn_mkdocs()


def restart_mkdocs() -> None:
    """Reinicia mkdocs siempre. Se usa tras conectar/reconectar, porque
    synced_docs/ puede haber sido borrado y re-clonado por completo."""
    kill_mkdocs()
    spawn_mkdocs()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    global startup_sync
    config = read_config()
    if config is None:
        return templates.TemplateResponse(
            request, "connect.html", {"error": None, "repo_url": ""}
        )
    has_token = keyring.get_password(KEYRING_SERVICE, KEYRING_KEY) is not None

    # El aviso del sync automatico de arranque se muestra una sola vez
    # (la primera carga de pantalla tras encender el proceso), no en
    # cada visita a "/".
    sync_message = sync_ok = None
    if startup_sync is not None:
        sync_ok, base_message = startup_sync
        sync_message = ("Sincronizado al arrancar. " if sync_ok else "No se pudo sincronizar al arrancar (se muestra la última copia local). ") + base_message
        startup_sync = None

    return templates.TemplateResponse(
        request,
        "connected.html",
        {
            "repo_url": config["repo_url"],
            "has_token": has_token,
            "wiki_url": f"http://{MKDOCS_HOST}:{MKDOCS_PORT}/",
            "sync_message": sync_message,
            "sync_ok": sync_ok,
        },
    )


@app.post("/connect", response_class=HTMLResponse)
def connect(request: Request, repo_url: str = Form(...), token: str = Form("")):
    repo_url = repo_url.strip()
    token = token.strip() or None

    if not repo_url.startswith("https://github.com/"):
        return templates.TemplateResponse(
            request,
            "connect.html",
            {
                "error": "Solo se soportan repositorios de GitHub (https://github.com/...) en esta versión.",
                "repo_url": repo_url,
            },
        )

    ok, message = run_sync(repo_url, token)
    if not ok:
        return templates.TemplateResponse(
            request,
            "connect.html",
            {"error": message, "repo_url": repo_url},
        )

    write_config(repo_url)
    if token:
        keyring.set_password(KEYRING_SERVICE, KEYRING_KEY, token)
    else:
        try:
            keyring.delete_password(KEYRING_SERVICE, KEYRING_KEY)
        except keyring.errors.PasswordDeleteError:
            pass

    restart_mkdocs()
    return RedirectResponse("/", status_code=303)


@app.post("/resync", response_class=HTMLResponse)
def resync(request: Request):
    config = read_config()
    if config is None:
        return RedirectResponse("/", status_code=303)
    token = keyring.get_password(KEYRING_SERVICE, KEYRING_KEY)
    ok, message = run_sync(config["repo_url"], token)
    has_token = token is not None
    if ok:
        ensure_mkdocs_running()
    return templates.TemplateResponse(
        request,
        "connected.html",
        {
            "repo_url": config["repo_url"],
            "has_token": has_token,
            "wiki_url": f"http://{MKDOCS_HOST}:{MKDOCS_PORT}/",
            "sync_message": message,
            "sync_ok": ok,
        },
    )


@app.post("/disconnect", response_class=HTMLResponse)
def disconnect(request: Request):
    clear_config()
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_KEY)
    except keyring.errors.PasswordDeleteError:
        pass
    # Se borra para que la proxima conexion siempre parta de un `clone`
    # limpio -- un `pull` contra un remoto distinto al original fallaria
    # (historias no relacionadas).
    shutil.rmtree(SYNCED_DOCS, ignore_errors=True)
    kill_mkdocs()
    return RedirectResponse("/", status_code=303)
