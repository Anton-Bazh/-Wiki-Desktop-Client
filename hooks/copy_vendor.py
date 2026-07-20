"""Copia `vendor/` (librerias de terceros vendorizadas -- mermaid.min.js,
chart.min.js, katex/ -- y marc-interactive.js, codigo propio compilado
desde frontend/, ver 02 Motores de backend.md) al `site_dir` en cada
build. Mismo patron y mismo motivo que `copy_branding.py`: `docs_dir` es
el symlink al repo activo, no un lugar seguro para assets propios de la
app.

Las librerias de terceros se vendorizan (en vez de dejar que
mkdocs-material las baje de un CDN en el navegador, su comportamiento por
defecto con Mermaid) para que la wiki funcione sin internet una vez
sincronizada -- ver `extra_javascript`/`extra_css` en mkdocs.yml.

Copia archivos Y subcarpetas: KaTeX no es un solo archivo, su CSS carga
~60 fuentes propias via `url(fonts/...)` relativo (`vendor/katex/`), asi
que la carpeta completa tiene que llegar al build, no solo los .js/.css
sueltos que bastaban para mermaid/chart.js."""

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
        elif f.is_dir():
            shutil.copytree(f, dest / f.name, dirs_exist_ok=True)
