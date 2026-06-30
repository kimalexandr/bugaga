#!/usr/bin/env bash
# Скопировать лицензию Callas с хоста в volume для Docker и перезапустить web.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p callas-license

HOST_LIC="/root/.callas software/callas pdfToolbox CLI 17/License.txt"
INSTALL_LIC="/opt/pdftoolbox/callas_pdfToolboxCLI_x64_Linux_17-0-682/License.txt"

if [ -f "$HOST_LIC" ]; then
  cp "$HOST_LIC" callas-license/License.txt
  echo "OK: лицензия скопирована из $HOST_LIC"
elif [ -f "$INSTALL_LIC" ]; then
  cp "$INSTALL_LIC" callas-license/License.txt
  echo "OK: лицензия скопирована из $INSTALL_LIC"
else
  echo "ERROR: лицензия не найдена. Сначала на хосте:"
  echo "  ./pdfToolbox --activate ~/activation/Activation.pdf"
  exit 1
fi

ls -la callas-license/License.txt

docker compose up -d --build
sleep 4

echo
echo "=== Callas --status в контейнере ==="
docker compose exec -T -w /opt/callas web ./pdfToolbox \
  --cachefolder=/var/callas-license --language=en --status 2>&1 | head -25

echo
echo "Дальше: bash scripts/diagnose-cmyk.sh"
