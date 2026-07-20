"""Genera una portada automatica cuando el repo conectado no trae su propia
`index.md`/`README.md` en la raiz. Sin esto, `/` devuelve 404 -- caso real:
el repo de Qalpix usa `00 Indice.md` como portada, no `index.md`.

Estrategia: redirigir `/` a la primera pagina del nav generado por MkDocs
(que, con convenciones de numeracion tipo Obsidian "00, 01, 02...", ya
coincide con la portada real del equipo. Si no hay convencion de numeros,
sigue siendo mejor que un 404 -- se abre la primera pagina disponible).
"""

def on_nav(nav, config, files):
    # Guardado en config.extra (por-build, no modulo) para que dos builds
    # concurrentes -- ej. conectar dos repos casi al mismo tiempo -- no se
    # pisen entre si (mismo patron que expose_active_repo.py).
    config.extra["_first_page_url"] = nav.pages[0].url if nav.pages else None
    return nav


def on_post_build(config):
    from pathlib import Path

    first_page_url = config.extra.get("_first_page_url")
    if first_page_url is None:
        return

    site_dir = Path(config["site_dir"])
    index_path = site_dir / "index.html"
    if index_path.exists():
        # El repo ya trae su propia portada (index.md o README.md).
        return

    # La pagina de redirect es una navegacion completa real (el logo del
    # header apunta aqui) -- sin estilo propio, el navegador mostraba su
    # lienzo default (negro en tema oscuro) mas el texto "Redirigiendo",
    # percibido como un parpadeo negro al volver al inicio de la wiki. Se
    # pinta el mismo fondo por esquema que el resto de las paginas y el
    # texto solo aparece si el redirect tarda de verdad (fallback).
    index_path.write_text(
        f"""<!doctype html>
<meta charset="utf-8">
<meta name="color-scheme" content="light dark">
<title>{config["site_name"]}</title>
<style>
  html {{ background: #fff; }}
  @media (prefers-color-scheme: dark) {{ html {{ background: #1e2129; }} }}
  p {{ font-family: system-ui, sans-serif; opacity: 0; animation: aparecer 0s 1.5s forwards; }}
  @keyframes aparecer {{ to {{ opacity: 1; }} }}
</style>
<meta http-equiv="refresh" content="0; url={first_page_url}">
<link rel="canonical" href="{first_page_url}">
<p>Redirigiendo a <a href="{first_page_url}">{first_page_url}</a>...</p>
""",
        encoding="utf-8",
    )
