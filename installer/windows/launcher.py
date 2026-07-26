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
"""

import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

INSTALL_DIR = Path(__file__).resolve().parent
PYTHON_EXE = INSTALL_DIR / "python" / "python.exe"
HOST, PORT = "127.0.0.1", 8766
URL = f"http://{HOST}:{PORT}/"

LOG_DIR = Path(os.environ.get("LOCALAPPDATA", INSTALL_DIR)) / "MARC"
LOG_PATH = LOG_DIR / "marc.log"


def is_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, PORT)) == 0


def start_backend() -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    log = open(LOG_PATH, "a", buffering=1, encoding="utf-8")
    log.write(f"\n--- arranque {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    return subprocess.Popen(
        [str(PYTHON_EXE), "-s", "-m", "uvicorn", "webapp.server:app", "--host", HOST, "--port", str(PORT)],
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
        return subprocess.Popen([firefox, "--no-remote", "--new-instance", url])
    webbrowser.open(url)
    return None


def main() -> None:
    backend = None
    if not is_running():
        backend = start_backend()
        for _ in range(60):
            if is_running():
                break
            time.sleep(0.5)
        else:
            sys.stderr.write(f"MARC: el backend no respondio a tiempo. Revisa {LOG_PATH}\n")

    window = open_window(URL)
    if window is not None and backend is not None:
        window.wait()
        backend.terminate()
        try:
            backend.wait(timeout=5)
        except subprocess.TimeoutExpired:
            backend.kill()


if __name__ == "__main__":
    main()
