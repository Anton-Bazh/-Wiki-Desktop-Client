"""Expone el nombre del repo activo a las plantillas de MkDocs
(`config.extra.active_repo_name`), leyendo directamente `webapp/config.json`
(la fuente de verdad del backend -- ver `webapp/server.py`). Sin esto,
MkDocs no tiene forma de saber que repo esta sirviendo: `site_name` es
fijo ("MARC") porque es el nombre de la app, no del
contenido conectado. Lo usa `theme_overrides/partials/path.html` para
la ruta bajo el header (ej. "QALPIX-DOC / wiki / 00-indice").
"""

import json
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _repo_display_name(repo_url: str) -> str:
    match = re.match(r"^https://github\.com/[^/]+/([^/]+?)(\.git)?/?$", repo_url)
    return match.group(1) if match else repo_url


def on_config(config, **kwargs):
    config_path = PROJECT_ROOT / "webapp" / "config.json"
    name = "wiki"
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))
        active_id = data.get("active_id")
        repo = next((r for r in data.get("repos", []) if r["id"] == active_id), None)
        if repo:
            name = _repo_display_name(repo["repo_url"])
    config.extra["active_repo_name"] = name

    version_path = PROJECT_ROOT / "VERSION"
    config.extra["app_version"] = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else "?"
    return config
