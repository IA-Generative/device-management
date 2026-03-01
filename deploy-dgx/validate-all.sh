#!/usr/bin/env sh
set -eu

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_TOOL="$ROOT_DIR/deploy-dgx/env-tool.sh"
COMPOSE_FILE="$ROOT_DIR/deploy-dgx/docker-compose.yml"

BASE_URL="${BASE_URL:-http://localhost:3001/bootstrap}"

say() { printf "%s\n" "$*"; }

check_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    say "SKIP: $1 not found"
    return 1
  fi
  return 0
}

check_env_alignment() {
  if [ -x "$ENV_TOOL" ]; then
    "$ENV_TOOL" check || true
  else
    say "SKIP: env-tool.sh not found"
  fi
}

docker_up() {
  if ! check_cmd docker; then
    return 0
  fi
  say "Docker: starting services..."
  docker compose -f "$COMPOSE_FILE" up -d --build
}

test_healthz() {
  if ! check_cmd curl; then
    return 0
  fi
  say "HTTP: /healthz"
  curl -sS -H 'Accept: application/problem+json' "$BASE_URL/healthz" || true
}

test_config() {
  if ! check_cmd curl || ! check_cmd python; then
    return 0
  fi
  say "HTTP: /config/config.json (updateUrl)"
  curl -sS "$BASE_URL/config/config.json" | python -c 'import json,sys; print(json.load(sys.stdin).get("updateUrl"))' || true
}

test_binaries() {
  if ! check_cmd curl; then
    return 0
  fi
  say "HTTP: /binaries (status + redirect)"
  curl -sS -o /dev/null -D - "$BASE_URL/binaries/matisse/evolution.png" | sed -n '1,5p' || true
}

k8s_smoke() {
  if ! check_cmd kubectl; then
    return 0
  fi
  say "Kubernetes: basic status (namespace bootstrap)"
  kubectl -n bootstrap get pods -l app=device-management || true
  kubectl -n bootstrap rollout status deploy/device-management || true
}

check_env_alignment
docker_up
test_healthz
test_config
test_binaries
k8s_smoke

say "Done."
