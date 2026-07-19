"""Genera una portada automatica cuando el repo conectado no trae su propia
`index.md`/`README.md` en la raiz. Sin esto, `/` devuelve 404 -- caso real:
el repo de Qalpix usa `00 Indice.md` como portada, no `index.md`.

Estrategia: redirigir `/` a la primera pagina del nav generado por MkDocs
(que, con convenciones de numeracion tipo Obsidian "00, 01, 02...", ya
coincide con la portada real del equipo. Si no hay convencion de numeros,
sigue siendo mejor que un 404 -- se abre la primera pagina disponible).
"""

_first_page_url = None


def on_nav(nav, config, files):
    global _first_page_url
    _first_page_url = None
    if nav.pages:
        _first_page_url = nav.pages[0].url
    return nav


def on_post_build(config):
    from pathlib import Path

    if _first_page_url is None:
        return

    site_dir = Path(config["site_dir"])
    index_path = site_dir / "index.html"
    if index_path.exists():
        # El repo ya trae su propia portada (index.md o README.md).
        return

    index_path.write_text(
        f"""<!doctype html>
<meta charset="utf-8">
<title>Wiki Desktop Client</title>
<meta http-equiv="refresh" content="0; url={_first_page_url}">
<link rel="canonical" href="{_first_page_url}">
<p>Redirigiendo a <a href="{_first_page_url}">{_first_page_url}</a>...</p>
"""
    )
