#!/usr/bin/env bash
# Скопировать лицензию Callas с хоста в volume для Docker и перезапустить web.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST_LIC="/root/.callas software/callas pdfToolbox CLI 17/License.txt"
INSTALL_LIC="/opt/pdftoolbox/callas_pdfToolboxCLI_x64_Linux_17-0-682/License.txt"
CACHE_SUBDIR="callas software/callas pdfToolbox CLI 17"

rm -rf callas-license
mkdir -p "callas-license/${CACHE_SUBDIR}"

if [ -f "$HOST_LIC" ]; then
  cp "$HOST_LIC" "callas-license/${CACHE_SUBDIR}/License.txt"
  echo "OK: лицензия скопирована из $HOST_LIC"
elif [ -f "$INSTALL_LIC" ]; then
  cp "$INSTALL_LIC" "callas-license/${CACHE_SUBDIR}/License.txt"
  echo "OK: лицензия скопирована из $INSTALL_LIC"
else
  echo "ERROR: лицензия не найдена. Сначала на хосте:"
  echo "  ./pdfToolbox --activate ~/activation/Activation.pdf"
  exit 1
fi

ls -la "callas-license/${CACHE_SUBDIR}/License.txt"

docker compose up -d --build
sleep 4

echo
echo "=== Callas --status в контейнере (cachefolder) ==="
docker compose exec -T -w /opt/callas web ./pdfToolbox \
  --cachefolder=/var/callas-license --status 2>&1 | head -20

echo
echo "=== Callas --status (mount ~/.callas software) ==="
docker compose exec -T -w /opt/callas web ./pdfToolbox --status 2>&1 | head -10 || true

echo
echo "Дальше: bash scripts/diagnose-cmyk.sh"
