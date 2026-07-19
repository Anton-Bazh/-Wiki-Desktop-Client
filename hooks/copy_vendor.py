"""Copia `vendor/` (librerias de terceros vendorizadas, ej. mermaid.min.js
-- ver 02 Motores de backend.md) al `site_dir` en cada build. Mismo patron
y mismo motivo que `copy_branding.py`: `docs_dir` es el symlink al repo
activo, no un lugar seguro para assets propios de la app.

mermaid.min.js se vendoriza (en vez de dejar que mkdocs-material lo baje
de unpkg.com en el navegador, su comportamiento por defecto) para que la
wiki funcione sin internet una vez sincronizada -- ver `extra_javascript`
en mkdocs.yml.
"""

import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def on_post_build(config, **kwargs):
    src = PROJECT_ROOT / "vendor"
    if not src.is_dir():
        return
    dest = Path(config["site_dir"]) / "vendor"
    dest.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file():
            shutil.copy2(f, dest / f.name)
