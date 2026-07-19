#!/usr/bin/env python3
"""Motor de sincronizacion (prototipo). Al arrancar la app, esto es lo que
Tauri/Rust deberia ejecutar antes de levantar `mkdocs serve`: clonar el repo
central la primera vez, o hacer pull si ya existe una copia local.

Prototipo con el `git` del sistema via subprocess, solo para validar el flujo
end-to-end en el navegador. La version final usa `git2-rs` embebido (ver
02 Motores de backend.md) para no depender de que el usuario tenga git
instalado.
"""

import subprocess
import sys
from pathlib import Path

REPO_URL = sys.argv[1] if len(sys.argv) > 1 else "../wiki-desktop-client-central.git"
SYNCED_DOCS = Path(__file__).parent / "synced_docs"


def run(*args, cwd=None):
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(result.returncode)
    return result.stdout.strip()


def main():
    if not (SYNCED_DOCS / ".git").exists():
        print(f"[sync] Clonando {REPO_URL} -> {SYNCED_DOCS}")
        run("git", "clone", REPO_URL, str(SYNCED_DOCS))
    else:
        print(f"[sync] Repo ya existe, haciendo pull en {SYNCED_DOCS}")
        before = run("git", "rev-parse", "HEAD", cwd=SYNCED_DOCS)
        run("git", "pull", "--ff-only", cwd=SYNCED_DOCS)
        after = run("git", "rev-parse", "HEAD", cwd=SYNCED_DOCS)
        if before == after:
            print("[sync] Ya estaba al dia, sin cambios nuevos")
        else:
            print(f"[sync] Actualizado: {before[:7]} -> {after[:7]}")


if __name__ == "__main__":
    main()
