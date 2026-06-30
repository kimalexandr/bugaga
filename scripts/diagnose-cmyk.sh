#!/usr/bin/env bash
# Диагностика CMYK через Callas внутри Docker-контейнера web.
# Использование:
#   bash scripts/diagnose-cmyk.sh
#   bash scripts/diagnose-cmyk.sh 5729a63a747541629c00a03e0f75014b
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

SID="${1:-}"
CMYK_PROFILE="Convert to CMYK only (ISO Coated v2 (ECI)).kfpx"

echo "=== 1. Health API ==="
curl -sf http://127.0.0.1:8000/health | python3 -m json.tool 2>/dev/null || echo "(API не отвечает)"
echo

if [ -z "$SID" ]; then
  SID="$(docker compose exec -T web sh -c 'ls -t /tmp/vizitka-sessions 2>/dev/null | head -1' | tr -d '\r')"
fi
echo "=== 2. Сессия: ${SID:-не найдена} ==="
if [ -z "$SID" ]; then
  echo "Нет сессий в /tmp/vizitka-sessions. Загрузите PDF в визард."
  exit 1
fi

docker compose exec -T web ls -la "/tmp/vizitka-sessions/${SID}/" 2>/dev/null || {
  echo "Папка сессии не найдена: $SID"
  exit 1
}
echo

echo "=== 3. meta.json ==="
docker compose exec -T web cat "/tmp/vizitka-sessions/${SID}/meta.json" 2>/dev/null | python3 -m json.tool 2>/dev/null || true
echo

WORKING="/tmp/vizitka-sessions/${SID}/working.pdf"
if ! docker compose exec -T web test -f "$WORKING" 2>/dev/null; then
  echo "ERROR: нет $WORKING"
  exit 1
fi

echo "=== 4. Профиль CMYK в Callas ==="
docker compose exec -T web sh -c "find /opt/callas/var/Profiles -iname '*CMYK*ISO*' -name '*.kfpx' 2>/dev/null | head -5"
echo

echo "=== 5. Лицензия Callas ==="
docker compose exec -T web sh -c 'find /opt/callas -maxdepth 3 \( -iname "*.lic" -o -iname "*license*" \) 2>/dev/null | head -10' || true
echo

echo "=== 6. Запуск CMYK профиля ==="
set +e
docker compose exec -T -w /opt/callas web ./pdfToolbox --language=ru \
  -o=/tmp/test_cmyk.pdf \
  "/opt/callas/var/Profiles/${CMYK_PROFILE}" \
  "$WORKING" 2>&1
EXIT=$?
set -e
echo
echo "exit: $EXIT"

docker compose exec -T web ls -la /tmp/test_cmyk.pdf 2>/dev/null \
  && echo "OK: test_cmyk.pdf создан" \
  || echo "FAIL: test_cmyk.pdf не создан"

echo
echo "=== 7. Логи web (CMYK) ==="
docker compose logs web --tail 80 2>/dev/null | grep -i cmyk || echo "(нет строк CMYK в последних 80 строках)"

echo
case "$EXIT" in
  0|1|2|5|6|7|8) echo "Код $EXIT — профиль отработал (возможны предупреждения)." ;;
  101) echo "Код 101 — чаще всего лицензия Callas или критическая ошибка профиля." ;;
  *) echo "Код $EXIT — ошибка Callas, см. вывод выше." ;;
esac
