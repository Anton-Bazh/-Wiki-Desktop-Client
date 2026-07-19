"""Normaliza las URLs servidas sin tocar los archivos del repo.

Los equipos escriben en Obsidian con nombres de archivo "naturales"
(espacios, mayusculas, acentos: "01 Vision y modelo de negocio.md").
MkDocs, por defecto, usa el nombre de archivo tal cual (URL-encoded)
como URL -- "notas/Nueva%20Nota%20del%20Compa%C3%B1ero/", que funciona
pero es feo y fragil para compartir. Este hook reescribe solo el
`dest_uri` (a donde se publica cada pagina) a un slug ascii en
minusculas; el archivo fuente en synced_docs/ nunca se modifica, asi
que un `git pull` nunca choca con esto.

Corre en `on_files`, antes de que se resuelvan los links relativos y se
construya la navegacion, asi que tanto los wikilinks (ver ezlinks en
02 Motores de backend.md) como el nav automatico usan las URLs ya
normalizadas sin que haya que tocar nada mas.
"""

import logging
import re
import unicodedata

log = logging.getLogger("mkdocs.plugins.slugify_urls")

_seen_dest_uris: set[str] = set()


def _slugify_segment(segment: str) -> str:
    stem, dot, ext = segment.rpartition(".")
    if not dot:
        stem, ext = segment, None
    normalized = unicodedata.normalize("NFKD", stem)
    ascii_stem = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_stem).strip("-").lower()
    slug = slug or "pagina"
    return f"{slug}.{ext}" if ext is not None else slug


def on_files(files, config):
    _seen_dest_uris.clear()
    for file in files.documentation_pages():
        # Acceder a dest_uri dispara el calculo original de MkDocs
        # (maneja index.html, directory urls, etc.); solo normalizamos
        # cada segmento del resultado, no reimplementamos esa logica.
        original = file.dest_uri
        slug_segments = [_slugify_segment(s) for s in original.split("/")]
        candidate = "/".join(slug_segments)

        if candidate in _seen_dest_uris:
            last = slug_segments[-1]
            stem, dot, ext = last.rpartition(".")
            suffix = 2
            while candidate in _seen_dest_uris:
                new_last = f"{stem}-{suffix}.{ext}" if dot else f"{last}-{suffix}"
                candidate = "/".join(slug_segments[:-1] + [new_last])
                suffix += 1
            log.warning(
                f"[slugify_urls] Colision de URL normalizada para '{file.src_uri}': "
                f"se uso '{candidate}' en su lugar."
            )

        _seen_dest_uris.add(candidate)
        file.dest_uri = candidate
    return files
