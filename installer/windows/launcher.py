"""Lanzador de MARC para el instalable de Windows.

Pensado para correr con pythonw.exe (sin ventana de consola). Si el
backend ya esta corriendo (ej. el usuario ya lo abrio antes y solo cerro
la pestana del navegador), no lo vuelve a lanzar -- solo abre el
navegador apuntando a el. PYTHONNOUSERSITE=1 y -s para el subproceso:
sin esto, el Python empaquetado puede terminar leyendo site-packages de
usuario de la maquina (ver 11 Camino a un instalable real, hallazgo de
contaminacion de site-packages).

stdout/stderr del backend van a %LOCALAPPDATA%\\MARC\\marc.log (analogo
al launcher.py de Linux) -- sin esto, un fallo al arrancar era invisible
por completo: pythonw.exe no tiene consola, y el navegador se abria
igual apuntando a un puerto muerto, sin ninguna pista de la causa.

Ventana propia en vez de una pestana mas del navegador (21-jul-2026,
mismo cambio que installer/linux/launcher.py -- ver ese archivo para el
porque). Edge viene instalado por defecto en Windows 10/11, asi que
`--app=` casi siempre aplica ahi; sin rutas de Chrome/Edge encontradas
cae a Firefox en instancia separada, y en ultimo caso a webbrowser.open()
de siempre. NOTA: sin verificar en Windows real todavia (solo Wine, que
no trae estos navegadores) -- ver "Pendiente" del proyecto.

Puerto libre automatico (Antonio, 29-jul-2026, mismo cambio y mismo
porque que installer/linux/launcher.py): el 8766 fijo se reemplaza por
`resolve_port()`, que reusa el puerto de la corrida anterior si todavia
responde COMO MARC (`/_marc_ping`) y si no busca uno libre de verdad
(bind real) desde el default.

Perfil de Firefox propio (Antonio, 1-ago-2026, mismo bug real y mismo
fix que installer/linux/launcher.py): el fallback a Firefox con
`--new-instance` pero sin `-P`/`-profile` seguia apuntando al perfil
default -- el mismo que un Firefox normal ya abierto del usuario. Dos
procesos sobre el mismo perfil chocan por su archivo de bloqueo y
Firefox muestra "Firefox is already running, but is not responding".
`_ensure_firefox_profile()` crea (una sola vez) un perfil separado
llamado "marc" via `-CreateProfile` para evitar el choque.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent
PYTHON_EXE = INSTALL_DIR / "python" / "python.exe"
HOST = "127.0.0.1"
DEFAULT_PORT = 8766

LOG_DIR = Path(os.environ.get("LOCALAPPDATA", INSTALL_DIR)) / "MARC"
LOG_PATH = LOG_DIR / "marc.log"
PORT_PATH = LOG_DIR / "port"
FIREFOX_PROFILE_NAME = "marc"
FIREFOX_PROFILE_MARKER = LOG_DIR / "firefox-profile-created"


def is_running(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, port)) == 0


def _port_free(port: int) -> bool:
    """True si `port` se puede enlazar ahora mismo -- una prueba real de
    bind, no solo de conexion: un puerto puede rechazar la conexion de
    is_running() (nadie escuchando todavia) y aun asi no estar libre para
    que uvicorn lo tome (ej. en TIME_WAIT de un proceso que acaba de
    morir)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((HOST, port))
            return True
        except OSError:
            return False


def _is_marc(port: int) -> bool:
    """True si lo que responde en `port` es esta misma app -- para no
    confundir un servicio ajeno que resulte estar escuchando ahi (el caso
    real que reporto Antonio: otro proceso ya tenia el 8766 y no se podia
    matar) con una instancia propia ya corriendo de una sesion anterior."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/_marc_ping", timeout=0.5) as resp:
            return json.loads(resp.read()).get("app") == "marc"
    except Exception:
        return False


def _find_free_port(start: int, tries: int = 200) -> int:
    for port in range(start, start + tries):
        if _port_free(port):
            return port
    # Ultimo recurso si 200 puertos seguidos estan ocupados (practicamente
    # imposible en una maquina de un solo usuario): que el SO asigne uno.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return s.getsockname()[1]


def resolve_port() -> int:
    """Que puerto usar en esta corrida: si el de la vez anterior
    (PORT_PATH) sigue respondiendo como MARC, se reusa -- is_running()
    en main() lo detecta corriendo y no se relanza el backend. Si no
    (primera vez, o el default/guardado esta ocupado por otra cosa que
    no se puede matar), se busca uno libre de verdad desde DEFAULT_PORT
    y se recuerda para la proxima."""
    if PORT_PATH.exists():
        try:
            saved = int(PORT_PATH.read_text().strip())
            if _is_marc(saved):
                return saved
        except (ValueError, OSError):
            pass
    port = _find_free_port(DEFAULT_PORT)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PORT_PATH.write_text(str(port), encoding="utf-8")
    return port


def start_backend(port: int) -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    log = open(LOG_PATH, "a", buffering=1, encoding="utf-8")
    log.write(f"\n--- arranque {time.strftime('%Y-%m-%d %H:%M:%S')} (puerto {port}) ---\n")
    return subprocess.Popen(
        [str(PYTHON_EXE), "-s", "-m", "uvicorn", "webapp.server:app", "--host", HOST, "--port", str(port)],
        cwd=str(INSTALL_DIR),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        close_fds=True,
    )


def _find_app_browser() -> str | None:
    found = shutil.which("msedge") or shutil.which("chrome") or shutil.which("google-chrome")
    if found:
        return found
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    for candidate in (
        os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local_appdata, "Google", "Chrome", "Application", "chrome.exe"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def _ensure_firefox_profile(firefox: str) -> bool:
    """Crea, una sola vez, un perfil de Firefox separado del perfil
    default del usuario -- ver la nota del modulo. True si el perfil
    quedo listo para usarse con `-P FIREFOX_PROFILE_NAME`; False si
    `-CreateProfile` fallo, en cuyo caso se cae al comportamiento viejo."""
    if FIREFOX_PROFILE_MARKER.exists():
        return True
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [firefox, "-CreateProfile", FIREFOX_PROFILE_NAME],
            timeout=15,
            capture_output=True,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    if result.returncode != 0:
        return False
    FIREFOX_PROFILE_MARKER.write_text("1", encoding="utf-8")
    return True


def open_window(url: str) -> subprocess.Popen | None:
    """Abre `url` en una ventana propia y devuelve el proceso para poder
    esperar su cierre -- None si solo pudimos caer al `webbrowser.open()`
    de siempre (sin navegador rastreable, el backend queda huerfano igual
    que antes)."""
    browser = _find_app_browser()
    if browser:
        return subprocess.Popen([browser, f"--app={url}"])
    firefox = shutil.which("firefox")
    if firefox:
        args = [firefox, "--no-remote", "--new-instance"]
        if _ensure_firefox_profile(firefox):
            args += ["-P", FIREFOX_PROFILE_NAME]
        args.append(url)
        return subprocess.Popen(args)
    webbrowser.open(url)
    return None


def main() -> None:
    port = resolve_port()
    url = f"http://{HOST}:{port}/"
    backend = None
    if not is_running(port):
        backend = start_backend(port)
        for _ in range(60):
            if is_running(port):
                break
            time.sleep(0.5)
        else:
            sys.stderr.write(f"MARC: el backend no respondio a tiempo. Revisa {LOG_PATH}\n")

    window = open_window(url)
    if window is not None and backend is not None:
        window.wait()
        backend.terminate()
        try:
            backend.wait(timeout=5)
        except subprocess.TimeoutExpired:
            backend.kill()


if __name__ == "__main__":
    main()
