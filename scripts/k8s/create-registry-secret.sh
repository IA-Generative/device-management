#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

PROFILE="${1:-}"
if [ -z "$PROFILE" ]; then
  echo "Usage: $0 <local|scaleway|dgx>" >&2
  exit 1
fi

case "$PROFILE" in
  local|scaleway|dgx) ;;
  *)
    echo "ERROR: profile must be one of: local|scaleway|dgx" >&2
    exit 1
    ;;
esac

require_cmd kubectl

ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${REGISTRY_ENV_FILE:-$ROOT_DIR/.env.registry}"
if [ ! -f "$ENV_FILE" ] && [ -f "$ROOT_DIR/deploy-dgx/.env.registry" ]; then
  ENV_FILE="$ROOT_DIR/deploy-dgx/.env.registry"
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: missing registry env file: $ENV_FILE" >&2
  echo "Create it from .env.registry.example (or deploy-dgx/.env.registry.example)." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

NAMESPACE="${REGISTRY_NAMESPACE:-bootstrap}"
SECRET_NAME="${REGISTRY_SECRET_NAME:-regcred}"
PROVIDER="$(printf "%s" "${REGISTRY_PROVIDER:-}" | tr '[:upper:]' '[:lower:]')"
if [ -z "$PROVIDER" ]; then
  case "$PROFILE" in
    scaleway|dgx) PROVIDER="scaleway" ;;
    local) PROVIDER="dockerhub" ;;
  esac
fi

SERVER=""
USERNAME=""
PASSWORD=""
EMAIL="${REGISTRY_EMAIL:-}"

case "$PROVIDER" in
  dockerhub)
    SERVER="${REGISTRY_SERVER:-https://index.docker.io/v1/}"
    USERNAME="${REGISTRY_USERNAME:-}"
    PASSWORD="${REGISTRY_PASSWORD:-}"
    if [ -z "$USERNAME" ] || [ -z "$PASSWORD" ]; then
      echo "ERROR: dockerhub needs REGISTRY_USERNAME and REGISTRY_PASSWORD in $ENV_FILE" >&2
      exit 1
    fi
    ;;
  scaleway)
    SERVER="${REGISTRY_SERVER:-rg.fr-par.scw.cloud/funcscwnspricelessmontalcinhiacgnzi}"
    USERNAME="${REGISTRY_USERNAME:-nologin}"
    PASSWORD="${REGISTRY_PASSWORD:-${SCW_SECRET_KEY:-}}"
    if [ -z "$PASSWORD" ]; then
      echo "ERROR: scaleway needs REGISTRY_PASSWORD (or SCW_SECRET_KEY) in $ENV_FILE" >&2
      exit 1
    fi
    ;;
  custom)
    SERVER="${REGISTRY_SERVER:-}"
    USERNAME="${REGISTRY_USERNAME:-}"
    PASSWORD="${REGISTRY_PASSWORD:-}"
    if [ -z "$SERVER" ] || [ -z "$USERNAME" ] || [ -z "$PASSWORD" ]; then
      echo "ERROR: custom needs REGISTRY_SERVER, REGISTRY_USERNAME and REGISTRY_PASSWORD in $ENV_FILE" >&2
      exit 1
    fi
    ;;
  *)
    echo "ERROR: unsupported REGISTRY_PROVIDER='$PROVIDER' (expected dockerhub|scaleway|custom)" >&2
    exit 1
    ;;
esac

kubectl -n "$NAMESPACE" delete secret "$SECRET_NAME" --ignore-not-found >/dev/null 2>&1 || true

cmd=(
  kubectl -n "$NAMESPACE" create secret docker-registry "$SECRET_NAME"
  --docker-server="$SERVER"
  --docker-username="$USERNAME"
  --docker-password="$PASSWORD"
)
if [ -n "$EMAIL" ]; then
  cmd+=(--docker-email="$EMAIL")
fi

"${cmd[@]}" >/dev/null
echo "Created/updated image pull secret '$SECRET_NAME' in namespace '$NAMESPACE' (provider=$PROVIDER, server=$SERVER)."
