#!/usr/bin/env bash
# Construye el paquete .deb de MARC (rama installer/nsis-browser).
#
# Analogo a installer/windows/marc.nsi: empaqueta el Python autocontenido
# (runtime/linux-x86_64/python, python-build-standalone -- ver .gitignore)
# junto con webapp/hooks/branding/vendor/theme_overrides bajo /opt/marc,
# root-owned y de solo lectura. El estado que la app escribe en caliente
# (config.json, repos/, etc.) NO vive ahi -- webapp/server.py detecta que
# /opt/marc no es escribible y cae solo a ~/.local/share/marc (ver
# _project_root_writable() en webapp/server.py). No requiere sudo/fakeroot
# para construirse: dpkg-deb --root-owner-group fija la propiedad root:root
# en los metadatos del .deb sin necesitar privilegios en esta maquina.
#
# Requiere runtime/linux-x86_64/python ya presente (descargado/instalado
# aparte, no versionado -- mismo patron que runtime/windows-x86_64).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

if [ ! -x "runtime/linux-x86_64/python/bin/python3" ]; then
  echo "Falta runtime/linux-x86_64/python (Python autocontenido para Linux)." >&2
  echo "Ver runtime/requirements.lock.txt." >&2
  exit 1
fi

VERSION="$(cat VERSION)"
ARCH="amd64"
PKG_NAME="marc"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

APP_DIR="$STAGE/opt/marc"
mkdir -p "$APP_DIR" "$STAGE/usr/bin" "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/icons/hicolor/256x256/apps" "$STAGE/usr/share/icons/hicolor/scalable/apps" \
  "$STAGE/DEBIAN"

echo "Copiando runtime de Python..."
rsync -a --exclude='__pycache__' "runtime/linux-x86_64/python/" "$APP_DIR/python/"

echo "Copiando aplicacion..."
rsync -a --exclude='__pycache__' --exclude='config.json' --exclude='mkdocs.pid' \
  --exclude='page_index.json' "webapp/" "$APP_DIR/webapp/"
rsync -a --exclude='__pycache__' "hooks/" "$APP_DIR/hooks/"
rsync -a "branding/" "$APP_DIR/branding/"
rsync -a "vendor/" "$APP_DIR/vendor/"
rsync -a "theme_overrides/" "$APP_DIR/theme_overrides/"
cp mkdocs.yml VERSION "$APP_DIR/"
cp installer/linux/launcher.py "$APP_DIR/launcher.py"

echo "Comando 'marc' en el PATH..."
cat > "$STAGE/usr/bin/marc" <<'EOF'
#!/bin/sh
exec /opt/marc/python/bin/python3 -s /opt/marc/launcher.py "$@"
EOF
chmod +x "$STAGE/usr/bin/marc"

echo "Icono y entrada de escritorio..."
cp installer/linux/assets/marc.png "$STAGE/usr/share/icons/hicolor/256x256/apps/marc.png"
cp installer/linux/assets/marc.svg "$STAGE/usr/share/icons/hicolor/scalable/apps/marc.svg"
cp installer/linux/marc.desktop "$STAGE/usr/share/applications/marc.desktop"

echo "Ajustando permisos (root:root, sin escritura de grupo/otros)..."
chmod -R go-w "$STAGE/opt" "$STAGE/usr"

INSTALLED_SIZE="$(du -sk "$STAGE" | cut -f1)"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: $PKG_NAME
Version: $VERSION
Section: utils
Priority: optional
Architecture: $ARCH
Installed-Size: $INSTALLED_SIZE
Maintainer: Antonio Baeza <baezaantoniosacial01@gmail.com>
Description: MARC - visor multi-repo de wikis Markdown/Git
 Conecta uno o mas repositorios Git con documentacion Markdown/Obsidian
 y los navega desde una sola interfaz local (MkDocs + Material +
 FastAPI). Python autocontenido -- no depende de tener Python, git ni
 MkDocs instalados en el sistema.
EOF

mkdir -p dist
OUT="dist/${PKG_NAME}_${VERSION}_${ARCH}.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT"
echo "Listo: $OUT"
