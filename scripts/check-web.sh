#!/usr/bin/env bash
# Почему не отвечает http://127.0.0.1:8000/health
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "=== 1. Контейнер ==="
docker compose ps -a 2>/dev/null || docker-compose ps -a
echo

echo "=== 2. Кто слушает порт 8000 ==="
(ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null) | grep ':8000' || echo "(порт 8000 свободен — контейнер не пробросил порт или упал)"
echo

echo "=== 3. Старый uvicorn без Docker ==="
if [ -f .deploy_pid ]; then
  pid="$(cat .deploy_pid 2>/dev/null || true)"
  if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
    echo "WARN: процесс PID ${pid} (deploy-no-docker.sh) — конфликт с Docker на :8000"
    echo "  Остановить: kill ${pid} && rm -f .deploy_pid"
  else
    echo "PID ${pid:-?} не активен (.deploy_pid устарел)"
  fi
else
  echo "нет .deploy_pid"
fi
echo

echo "=== 4. Логи web (последние 50 строк) ==="
docker compose logs web --tail 50 2>/dev/null || docker-compose logs web --tail 50
echo

echo "=== 5. Health с хоста ==="
if curl -sv --max-time 8 http://127.0.0.1:8000/health 2>&1; then
  echo
else
  echo "(curl с хоста не получил ответ)"
fi
echo

echo "=== 6. Health изнутри контейнера ==="
docker compose exec -T web python -c "
import urllib.request
try:
    r = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=8)
    print(r.read().decode())
except Exception as e:
    print('FAIL:', e)
" 2>/dev/null || echo "(exec в контейнер не удался — контейнер не запущен)"
echo

echo "=== 7. Версия кода в контейнере ==="
docker compose exec -T web python -c "
import os
print('CALLAS_LANGUAGE=', os.getenv('CALLAS_LANGUAGE'))
from app.main import health
print(health())
" 2>/dev/null || true
