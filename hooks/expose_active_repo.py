"""Expone el nombre del repo que se esta construyendo a las plantillas de
MkDocs (`config.extra.active_repo_name`). Sin esto, MkDocs no tiene forma
de saber que repo esta sirviendo: `site_name` es fijo ("MARC") porque es
el nombre de la app, no del contenido conectado. Lo usa
`theme_overrides/partials/path.html` para la ruta bajo el header (ej.
"QALPIX-DOC / wiki / 00-indice").

`webapp/server.py::build_site()` ya calcula el nombre correcto para el
repo que esta construyendo en ese momento y lo pasa como `extra` a
`mkdocs_load_config()` -- este hook solo lo respeta si ya llego puesto.
El fallback de leer `webapp/config.json` (repo activo global) queda solo
para invocaciones fuera de ese camino (ej. `mkdocs build`/`mkdocs serve`
directo desde la CLI, sin pasar por build_site()) -- usarlo siempre aqui
fue el bug original: pre-construir el sitio de un repo que todavia no es
el activo (o el repo fijo de documentacion de uso, que nunca lo es por
diseno, ver `docs()`) heredaba el nombre de OTRO repo, el que fuera
activo en ese momento.
"""

import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _repo_display_name(repo_url: str) -> str:
    match = re.match(r"^https://github\.com/[^/]+/([^/]+?)(\.git)?/?$", repo_url)
    return match.group(1) if match else repo_url


def on_config(config, **kwargs):
    if not config.extra.get("active_repo_name"):
        config_path = PROJECT_ROOT / "webapp" / "config.json"
        name = "wiki"
        if config_path.exists():
            data = json.loads(config_path.read_text(encoding="utf-8"))
            active_id = data.get("active_id")
            repo = next((r for r in data.get("repos", []) if r["id"] == active_id), None)
            if repo:
                # Un repo local (ver connect_local() en webapp/server.py)
                # no tiene repo_url -- su nombre es el de la carpeta.
                name = Path(repo["local_path"]).name if "local_path" in repo else _repo_display_name(repo["repo_url"])
        config.extra["active_repo_name"] = name

    version_path = PROJECT_ROOT / "VERSION"
    config.extra["app_version"] = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else "?"
    return config
