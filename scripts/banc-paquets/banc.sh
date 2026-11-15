#!/bin/sh
# Rejoue le banc depuis zéro : clés neuves, image reconstruite, base vide.
set -e
cd "$(dirname "$0")"
# Interpréteur qui a les dépendances du dépôt, httpx et playwright (PY=… pour en changer).
PY="${PY:-../../.venv/bin/python}"
"$PY" gen.py "${1:-delegation}" >/dev/null
docker compose down -v >/dev/null 2>&1 || true
docker compose build admin >/dev/null 2>&1
docker compose up -d >/dev/null 2>&1
for i in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:18091/healthz)" = 200 ] && \
  [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:18090/healthz)" = 200 ] && break
  sleep 2
done
sleep 2
"$PY" run.py
"$PY" ui.py
