#!/usr/bin/env bash
# Проверка callas pdfToolbox CLI на сервере (не трогает bugaga).
# Использование:
#   bash scripts/test-callas.sh
#   bash scripts/test-callas.sh /path/to/визитка.pdf
#   CALLAS=/opt/pdftoolbox/callas_pdfToolboxCLI_x64_Linux_17-0-682 bash scripts/test-callas.sh визитка.pdf
set -euo pipefail

CALLAS="${CALLAS:-/opt/pdftoolbox/callas_pdfToolboxCLI_x64_Linux_17-0-682}"
PDF="${1:-}"
OUT="${OUT:-/tmp/callas-test}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

mkdir -p "$OUT/in" "$OUT/out"

if [ ! -x "$CALLAS/pdfToolbox" ]; then
  echo "ERROR: не найден $CALLAS/pdfToolbox"
  echo "Укажите путь: CALLAS=/opt/pdftoolbox/callas_pdfToolboxCLI_x64_Linux_17-0-682 bash $0 визитка.pdf"
  exit 1
fi

cd "$CALLAS"

echo "=== 1. CLI ==="
./pdfToolbox --help | head -5
echo

echo "=== 2. Библиотеки ==="
if ldd ./pdfToolbox 2>&1 | grep -q "not found"; then
  ldd ./pdfToolbox 2>&1 | grep "not found"
else
  echo "OK: все lib найдены"
fi
echo

echo "=== 3. Профили bleed / cmyk ==="
find ./var/Profiles \( -iname "*bleed*" -o -iname "*cmyk*" \) 2>/dev/null | head -20
echo

echo "=== 4. bugaga (если запущен) ==="
if curl -sf --max-time 2 http://127.0.0.1:8000/health >/dev/null 2>&1; then
  curl -s http://127.0.0.1:8000/health
  echo
else
  echo "bugaga на :8000 не отвечает (это нормально, если сервис не поднят)"
fi
echo

if [ -z "$PDF" ] || [ ! -f "$PDF" ]; then
  echo "PDF не передан или файл не найден."
  echo
  echo "Использование:"
  echo "  bash $ROOT/scripts/test-callas.sh /path/to/визитка.pdf"
  echo "  CALLAS=/opt/pdftoolbox/callas_pdfToolboxCLI_x64_Linux_17-0-682 bash $ROOT/scripts/test-callas.sh визитка.pdf"
  exit 0
fi

echo "=== 5. Инфо по PDF: $PDF ==="
./pdfToolbox --quickpdfinfo "$PDF"
echo

BLEED_PROFILE="$(find ./var/Profiles -iname 'Check and fix bleed.kfpx' 2>/dev/null | head -1)"
CMYK_PROFILE="$(find ./var/Profiles -path '*Convert colors*' -iname '*CMYK*ISO Coated v2*' 2>/dev/null | head -1)"
ENLARGE_PROFILE="$(find ./var/Profiles -iname 'Enlarge page at edges.kfpx' 2>/dev/null | head -1)"

if [ -n "$BLEED_PROFILE" ]; then
  echo "=== 6. Preflight (только анализ): $BLEED_PROFILE ==="
  set +e
  ./pdfToolbox --analyze --language=ru \
    -r=XML,ALWAYS,PATH="$OUT/out/preflight.xml" \
    "$BLEED_PROFILE" "$PDF"
  echo "exit code: $?"
  set -e
  head -30 "$OUT/out/preflight.xml" 2>/dev/null || true
  echo
else
  echo "=== 6. Preflight: профиль 'Check and fix bleed.kfpx' не найден ==="
  echo
fi

if [ -n "$ENLARGE_PROFILE" ]; then
  echo "=== 7. Растяжение +2 мм с каждой стороны: $ENLARGE_PROFILE ==="
  set +e
  ./pdfToolbox -o="$OUT/out/enlarged.pdf" \
    --setvariable=Add_mm_left:2 \
    --setvariable=Add_mm_right:2 \
    --setvariable=Add_mm_top:2 \
    --setvariable=Add_mm_bottom:2 \
    "$ENLARGE_PROFILE" "$PDF"
  echo "exit code: $?"
  set -e
  ./pdfToolbox --quickpdfinfo "$OUT/out/enlarged.pdf" 2>/dev/null || true
  echo
else
  echo "=== 7. Enlarge: профиль 'Enlarge page at edges.kfpx' не найден ==="
  echo
fi

if [ -n "$CMYK_PROFILE" ]; then
  echo "=== 8. RGB → CMYK: $CMYK_PROFILE ==="
  set +e
  ./pdfToolbox -o="$OUT/out/cmyk.pdf" "$CMYK_PROFILE" "$PDF"
  echo "exit code: $?"
  set -e
  echo
else
  echo "=== 8. CMYK: профиль Convert to CMYK (ISO Coated v2) не найден ==="
  echo
fi

echo "=== 9. Превью PNG ==="
set +e
./pdfToolbox --saveasimg --imgformat=PNG --resolution=400x220 \
  --pagebox=TRIMBOX -p=1 -o="$OUT/out/preview_p1.png" "$PDF"
echo "exit code: $?"
set -e
ls -la "$OUT/out/" 2>/dev/null || true
echo
echo "Готово. Результаты: $OUT/out/"
