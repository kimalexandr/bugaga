#!/usr/bin/env bash
# Деплой без Docker: venv + uvicorn (порт 8000), если на VPS нет docker.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="/usr/local/bin:/usr/bin:/bin:${PATH}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: нет python3. На Debian/Ubuntu: apt update && apt install -y python3 python3-venv python3-pip"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

mkdir -p logs
if [ -f .deploy_pid ]; then
  pid="$(cat .deploy_pid 2>/dev/null || true)"
  if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null || true
    sleep 1
  fi
fi

nohup ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 >>logs/uvicorn.log 2>&1 &
echo $! >.deploy_pid
echo "OK: uvicorn PID $(cat .deploy_pid), лог: ${ROOT}/logs/uvicorn.log"
