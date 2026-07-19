"""Lanzador de MARC para el instalable de Windows.

Pensado para correr con pythonw.exe (sin ventana de consola). Si el
backend ya esta corriendo (ej. el usuario ya lo abrio antes y solo cerro
la pestana del navegador), no lo vuelve a lanzar -- solo abre el
navegador apuntando a el. PYTHONNOUSERSITE=1 y -s para el subproceso:
sin esto, el Python empaquetado puede terminar leyendo site-packages de
usuario de la maquina (ver 11 Camino a un instalable real, hallazgo de
contaminacion de site-packages).
"""

import os
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


def is_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((HOST, PORT)) == 0


def start_backend() -> None:
    env = os.environ.copy()
    env["PYTHONNOUSERSITE"] = "1"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        [str(PYTHON_EXE), "-s", "-m", "uvicorn", "webapp.server:app", "--host", HOST, "--port", str(PORT)],
        cwd=str(INSTALL_DIR),
        env=env,
        creationflags=creationflags,
        close_fds=True,
    )


def main() -> None:
    if not is_running():
        start_backend()
        for _ in range(60):
            if is_running():
                break
            time.sleep(0.5)
    webbrowser.open(URL)


if __name__ == "__main__":
    main()
