"""Alinea el slug de anclas que genera ezlinks (plugin `mkdocs-obsidian-links`)
con el slug real que produce la extension `toc` de python-markdown.

ezlinks convierte '[[Nota#Sección]]' usando su propio slugify (solo minusculas
y espacios -> guiones, sin quitar acentos), pero el id real del encabezado en
el HTML final lo genera `toc.slugify` (quita acentos via NFKD). Sin este parche,
cualquier ancla a un encabezado con tildes queda rota.
"""

from markdown.extensions.toc import slugify
from mkdocs_obsidian_links.scanners.wiki_link_scanner import WikiLinkScanner


def _slugify_matching_toc(self, link: str) -> str:
    return slugify(link, "-")


WikiLinkScanner._slugify = _slugify_matching_toc
