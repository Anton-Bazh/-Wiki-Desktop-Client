#!/usr/bin/env bash
# Construye el instalador de Windows (MARC-Setup.exe) con makensis.
#
# Analogo a installer/linux/build-deb.sh: lee VERSION de la raiz del
# proyecto (fuente unica, la misma que webapp/server.py expone como
# APP_VERSION en runtime) y genera version.nsh -- marc.nsi ya no trae la
# version hardcodeada, la incluye de ahi (ver el comentario en marc.nsi).
# Sin este script, compilar el .nsi a mano dejaba el DisplayVersion del
# registro de Windows (Agregar o quitar programas) desincronizado del
# VERSION real empaquetado adentro, y de lo que "Acerca de" mostraba en
# la app -- las tres cosas ahora vienen del mismo archivo.
#
# Requiere runtime/windows-x86_64/python ya presente (descargado/instalado
# aparte, no versionado -- mismo patron que runtime/linux-x86_64), y
# makensis en el PATH (paquete nsis; funciona igual compilando en Linux
# nativo o en Windows real, el .exe resultante ya se probo con Wine).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [ ! -x "runtime/windows-x86_64/python/pythonw.exe" ] && [ ! -f "runtime/windows-x86_64/python/pythonw.exe" ]; then
  echo "Falta runtime/windows-x86_64/python (Python autocontenido para Windows)." >&2
  echo "Ver runtime/requirements.lock.txt." >&2
  exit 1
fi

if ! command -v makensis >/dev/null 2>&1; then
  echo "Falta makensis (paquete nsis) en el PATH." >&2
  exit 1
fi

VERSION="$(cat VERSION)"
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "VERSION invalido ('$VERSION'): debe ser MAYOR.MENOR.PARCHE, enteros separados por punto." >&2
  exit 1
fi

echo "!define VERSION \"$VERSION\"" > installer/windows/version.nsh

mkdir -p dist
makensis installer/windows/marc.nsi

echo "Listo: dist/MARC-Setup.exe (v$VERSION)"
