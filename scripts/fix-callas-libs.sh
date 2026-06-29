#!/usr/bin/env bash
# Восстановление lib callas pdfToolbox (пустые .so вместо symlink после копирования через FTP).
# Использование:
#   bash scripts/fix-callas-libs.sh
#   CALLAS=/opt/callas-pdftoolbox bash scripts/fix-callas-libs.sh
set -euo pipefail

CALLAS="${CALLAS:-/opt/pdftoolbox/callas_pdfToolboxCLI_x64_Linux_17-0-682}"
LIB="$CALLAS/lib"
ARCHIVE="$CALLAS/callas_pdfToolboxCLI_x64_Linux.tar.gz"

if [ ! -d "$LIB" ]; then
  echo "ERROR: нет папки $LIB"
  exit 1
fi

broken_count="$(find "$LIB" -name '*.so*' -size 0 2>/dev/null | wc -l)"
echo "Пустых .so в lib/: $broken_count"

if [ "$broken_count" -eq 0 ]; then
  echo "OK: битых lib не найдено"
  "$CALLAS/pdfToolbox" --help >/dev/null 2>&1 && echo "pdfToolbox запускается" || true
  exit 0
fi

echo "Примеры битых файлов:"
find "$LIB" -name '*.so*' -size 0 2>/dev/null | head -10
echo

if [ ! -f "$ARCHIVE" ]; then
  echo "ERROR: архив не найден: $ARCHIVE"
  echo "Залейте callas_pdfToolboxCLI_x64_Linux.tar.gz в $CALLAS и запустите снова."
  exit 1
fi

echo "=== Проверка архива ==="
gzip -t "$ARCHIVE" || { echo "ERROR: архив битый — скачайте заново с callas.com"; exit 1; }
echo "gzip OK ($(du -h "$ARCHIVE" | cut -f1))"
echo

echo "=== Переизвлечение lib/ и syslibs/ из tar (symlink сохраняются) ==="
tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

tar -xzf "$ARCHIVE" -C "$tmpdir"
src="$(find "$tmpdir" -maxdepth 1 -type d -name 'callas_*' 2>/dev/null | head -1)"
if [ -z "$src" ]; then
  src="$(find "$tmpdir" -mindepth 1 -maxdepth 2 -type d -name 'callas_*' 2>/dev/null | head -1)"
fi
if [ -z "$src" ] || [ ! -d "$src/lib" ]; then
  echo "ERROR: в архиве не найдена папка callas_*/lib"
  find "$tmpdir" -type d | head -20
  exit 1
fi

ts="$(date +%Y%m%d%H%M%S)"
bak="$CALLAS/lib.bak.$ts"
mv "$LIB" "$bak"
cp -a "$src/lib" "$LIB"
chmod -R a+rX "$LIB"
echo "lib/ восстановлена. Бэкап: $bak"

if [ -d "$src/syslibs" ]; then
  if [ -d "$CALLAS/syslibs" ]; then
    mv "$CALLAS/syslibs" "$CALLAS/syslibs.bak.$ts"
  fi
  cp -a "$src/syslibs" "$CALLAS/syslibs"
  chmod -R a+rX "$CALLAS/syslibs"
  echo "syslibs/ восстановлена"
fi

remaining="$(find "$LIB" -name '*.so*' -size 0 2>/dev/null | wc -l)"
echo "Пустых .so после восстановления: $remaining"

if [ "$remaining" -gt 0 ]; then
  echo "ERROR: lib всё ещё битые"
  find "$LIB" -name '*.so*' -size 0 | head -10
  exit 1
fi

echo
echo "=== Тест pdfToolbox ==="
if "$CALLAS/pdfToolbox" --help | head -3; then
  echo
  echo "OK: Callas работает"
else
  echo "ERROR: pdfToolbox не запустился — возможно бит и бинарник, переустановите всю папку из tar"
  exit 1
fi
