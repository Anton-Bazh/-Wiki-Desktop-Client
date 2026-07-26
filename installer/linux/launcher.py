"""Lanzador de MARC para el instalable de Linux (paquete .deb).

Analogo a installer/windows/launcher.py: si el backend ya esta corriendo
(el usuario ya lo habia abierto y solo cerro la pestana), no lo vuelve a
lanzar -- solo abre el navegador apuntando a el. PYTHONNOUSERSITE=1 y -s
para el subproceso, mismo motivo que en Windows (ver 11 Camino a un
instalable real, hallazgo de contaminacion de site-packages): sin esto,
el Python empaquetado puede leer site-packages de usuario de la maquina.

stdout/stderr del backend van a ~/.local/share/marc/marc.log (no hay
consola visible al lanzar desde el icono del escritorio) -- si el
navegador abre pero la pagina no carga, ese archivo es el primer lugar
donde mirar.

Ventana propia en vez de una pestana mas del navegador que ya tengas
abierto (Antonio, 21-jul-2026): antes `webbrowser.open()` reusaba el
navegador existente y no habia forma de saber cuando el usuario "cerraba
MARC" -- por diseno el backend quedaba corriendo indefinidamente aunque
se cerrara la pestana, ver doc del bug de sync (`/api/sync-check`).
`open_window()` prueba primero Chrome/Chromium/Edge/Brave en `--app=`
(ventana sin pestanas ni barra de direcciones, boton de cerrar normal);
si el unico navegador instalado es Firefox (el caso real en esta
maquina), cae a una instancia nueva y separada (`--no-remote
--new-instance`) en vez de agregar una pestana a un Firefox ya abierto
-- necesario para poder esperar a que ESA ventana se cierre sin
confundirla con otras pestanas del usuario. Solo cuando este lanzador
fue quien arranco el backend (no cuando ya estaba corriendo de una
sesion/ventana previa) se espera el cierre de la ventana para matarlo.
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
PYTHON_EXE = INSTALL_DIR / "python" / "bin" / "python3"
HOST, PORT = "127.0.0.1", 8766
URL = f"http://{HOST}:{PORT}/"

LOG_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "marc"
LOG_PATH = LOG_DIR / "marc.log"

APP_MODE_BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "microsoft-edge-stable",
    "brave-browser",
)


def is_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, PORT)) == 0


def start_backend() -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    log = open(LOG_PATH, "a", buffering=1)
    log.write(f"\n--- arranque {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    return subprocess.Popen(
        [str(PYTHON_EXE), "-s", "-m", "uvicorn", "webapp.server:app", "--host", HOST, "--port", str(PORT)],
        cwd=str(INSTALL_DIR),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )


def _find_app_browser() -> str | None:
    for name in APP_MODE_BROWSERS:
        path = shutil.which(name)
        if path:
            return path
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
