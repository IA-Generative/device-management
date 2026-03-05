#!/usr/bin/env sh
set -eu

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-${VALIDATE_MODE:-dgx}}"
NAMESPACE="${NAMESPACE:-bootstrap}"

say() { printf "%s\n" "$*"; }

usage() {
  cat <<EOF
Usage: ./deploy-dgx/validate-all.sh [local|dgx|all]

Modes:
  local  Validate local stack (infra-minimal docker-compose + local endpoints)
  dgx    Validate DGX stack (deploy-dgx env + compose if .env exists + k8s checks)
  all    Run both modes sequentially
EOF
}

check_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    say "SKIP: $1 not found"
    return 1
  fi
  return 0
}

configure_mode() {
  CURRENT_MODE="$1"
  case "$CURRENT_MODE" in
    local)
      ENV_TOOL="$ROOT_DIR/infra-minimal/env-tool.sh"
      COMPOSE_FILE="$ROOT_DIR/infra-minimal/docker-compose.yml"
      ENV_FILE="$ROOT_DIR/infra-minimal/.env"
      ENV_SECRETS_FILE="$ROOT_DIR/infra-minimal/.env.secrets"
      BASE_URL="${BASE_URL:-http://localhost:3001}"
      RELAY_BASE_URL="${RELAY_BASE_URL:-http://localhost:8088}"
      ;;
    dgx)
      ENV_TOOL="$ROOT_DIR/deploy-dgx/env-tool.sh"
      COMPOSE_FILE="$ROOT_DIR/deploy-dgx/docker-compose.yml"
      ENV_FILE="$ROOT_DIR/deploy-dgx/.env"
      ENV_SECRETS_FILE="$ROOT_DIR/deploy-dgx/.env.secrets"
      BASE_URL="${BASE_URL:-http://localhost:3001}"
      RELAY_BASE_URL="${RELAY_BASE_URL:-http://localhost:8088}"
      ;;
    *)
      say "ERROR: unsupported mode '$CURRENT_MODE'"
      usage
      exit 1
      ;;
  esac
}

check_env_alignment() {
  if [ -x "$ENV_TOOL" ]; then
    "$ENV_TOOL" check || true
  else
    say "SKIP: env-tool.sh not found for mode=$CURRENT_MODE"
  fi
}

docker_up() {
  if ! check_cmd docker; then
    return 0
  fi
  if [ ! -f "$ENV_FILE" ] || [ ! -f "$ENV_SECRETS_FILE" ]; then
    say "SKIP: docker compose for mode=$CURRENT_MODE (missing $ENV_FILE or $ENV_SECRETS_FILE)"
    return 0
  fi
  say "Docker: starting services (mode=$CURRENT_MODE)..."
  docker compose -f "$COMPOSE_FILE" up -d --build
}

test_healthz() {
  if ! check_cmd curl; then
    return 0
  fi
  say "HTTP: /healthz ($BASE_URL)"
  curl -sS -H 'Accept: application/problem+json' "$BASE_URL/healthz" || true
}

test_config() {
  if ! check_cmd curl || ! check_cmd python; then
    return 0
  fi
  say "HTTP: /config/config.json (updateUrl)"
  curl -sS "$BASE_URL/config/config.json" | python -c 'import json,sys; print(json.load(sys.stdin).get("updateUrl"))' || true
}

test_relay() {
  if ! check_cmd curl; then
    return 0
  fi
  say "HTTP: relay-assistant /healthz ($RELAY_BASE_URL)"
  curl -sS "$RELAY_BASE_URL/healthz" || true
  say "HTTP: relay-assistant keycloak denied without key (expect 401/403/404/405)"
  code="$(curl -sS -o /dev/null -w '%{http_code}' "$RELAY_BASE_URL/keycloak/protocol/openid-connect/token" || true)"
  echo "relay status=$code"
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
  say "Kubernetes: basic status (namespace $NAMESPACE)"
  kubectl -n "$NAMESPACE" get pods -l app=device-management || true
  kubectl -n "$NAMESPACE" get pods -l app=relay-assistant || true
  kubectl -n "$NAMESPACE" rollout status deploy/device-management || true
}

run_mode() {
  configure_mode "$1"
  say "== validate-all mode=$CURRENT_MODE =="
  check_env_alignment
  docker_up
  test_healthz
  test_config
  test_relay
  test_binaries
  k8s_smoke
  say "Done (mode=$CURRENT_MODE)."
}

case "$MODE" in
  local|dgx)
    run_mode "$MODE"
    ;;
  all)
    run_mode local
    run_mode dgx
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    say "ERROR: invalid mode '$MODE'"
    usage
    exit 1
    ;;
esac
