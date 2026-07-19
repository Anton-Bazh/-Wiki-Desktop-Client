"""Copia `branding/` (logos de la app, fuera de docs_dir a proposito --
ver 08 Recuperacion de sesion... y el comentario en `theme.logo` de
mkdocs.yml) al `site_dir` en cada build.

Necesario porque MkDocs no copia `theme.logo`/`theme.favicon` por su
cuenta: solo genera la URL relativa asumiendo que el archivo ya vive en
algun punto del site servido (normalmente porque el usuario lo puso
dentro de `docs_dir`, donde el paso-directo de estaticos de MkDocs si
lo copia). Como nuestro `docs_dir` es el symlink al repo activo -- no
un lugar seguro para assets propios de la app -- este hook hace ese
copiado a mano.
"""

import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def on_post_build(config, **kwargs):
    src = PROJECT_ROOT / "branding"
    if not src.is_dir():
        return
    dest = Path(config["site_dir"]) / "branding"
    dest.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file():
            shutil.copy2(f, dest / f.name)
