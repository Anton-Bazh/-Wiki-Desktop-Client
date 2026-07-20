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
"""

import os
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


def is_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, PORT)) == 0


def start_backend() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    log = open(LOG_PATH, "a", buffering=1)
    log.write(f"\n--- arranque {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    subprocess.Popen(
        [str(PYTHON_EXE), "-s", "-m", "uvicorn", "webapp.server:app", "--host", HOST, "--port", str(PORT)],
        cwd=str(INSTALL_DIR),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )


def main() -> None:
    if not is_running():
        start_backend()
        for _ in range(60):
            if is_running():
                break
            time.sleep(0.5)
        else:
            sys.stderr.write(f"MARC: el backend no respondio a tiempo. Revisa {LOG_PATH}\n")
    webbrowser.open(URL)


if __name__ == "__main__":
    main()
