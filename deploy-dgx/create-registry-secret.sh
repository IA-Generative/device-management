#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$ROOT_DIR/deploy-dgx/scripts/lib-dgx.sh"

SETTINGS_FILE="${1:-$ROOT_DIR/deploy-dgx/settings.yaml}"

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<'EOF'
Usage:
  ./deploy-dgx/create-registry-secret.sh [settings.yaml]
EOF
  exit 0
fi

if [ ! -f "$SETTINGS_FILE" ]; then
  echo "ERROR: missing settings file $SETTINGS_FILE" >&2
  exit 1
fi

require_cmd kubectl
confirm_kubectl_context

ensure_registry_env_file "$REGISTRY_ENV_FILE"
load_registry_env "$REGISTRY_ENV_FILE"

NAMESPACE_DEFAULT="$(namespace_from_settings "$SETTINGS_FILE")"
NAMESPACE="${REGISTRY_NAMESPACE:-$NAMESPACE_DEFAULT}"
SECRET_NAME="${REGISTRY_SECRET_NAME:-regcred}"
PROVIDER="$(printf "%s" "${REGISTRY_PROVIDER:-dockerhub}" | tr '[:upper:]' '[:lower:]')"

SERVER=""
USERNAME=""
PASSWORD=""
EMAIL=""

case "$PROVIDER" in
  dockerhub)
    SERVER="${REGISTRY_SERVER:-https://index.docker.io/v1/}"
    USERNAME="${REGISTRY_USERNAME:-}"
    PASSWORD="${REGISTRY_PASSWORD:-}"
    EMAIL="${REGISTRY_EMAIL:-}"
    if [ -z "$USERNAME" ] || [ -z "$PASSWORD" ]; then
      echo "ERROR: REGISTRY_USERNAME and REGISTRY_PASSWORD are required for dockerhub in $REGISTRY_ENV_FILE" >&2
      exit 1
    fi
    ;;
  scaleway)
    SERVER="${REGISTRY_SERVER:-rg.fr-par.scw.cloud/funcscwnspricelessmontalcinhiacgnzi}"
    USERNAME="${REGISTRY_USERNAME:-nologin}"
    PASSWORD="${REGISTRY_PASSWORD:-${SCW_SECRET_KEY:-}}"
    EMAIL="${REGISTRY_EMAIL:-}"
    if [ -z "$PASSWORD" ]; then
      echo "ERROR: REGISTRY_PASSWORD (or SCW_SECRET_KEY) is required for scaleway in $REGISTRY_ENV_FILE" >&2
      exit 1
    fi
    ;;
  custom)
    SERVER="${REGISTRY_SERVER:-}"
    USERNAME="${REGISTRY_USERNAME:-}"
    PASSWORD="${REGISTRY_PASSWORD:-}"
    EMAIL="${REGISTRY_EMAIL:-}"
    if [ -z "$SERVER" ] || [ -z "$USERNAME" ] || [ -z "$PASSWORD" ]; then
      echo "ERROR: REGISTRY_SERVER, REGISTRY_USERNAME and REGISTRY_PASSWORD are required for custom provider" >&2
      exit 1
    fi
    ;;
  *)
    echo "ERROR: unsupported REGISTRY_PROVIDER='$PROVIDER' (expected dockerhub|scaleway|custom)" >&2
    exit 1
    ;;
esac

kubectl -n "$NAMESPACE" delete secret "$SECRET_NAME" --ignore-not-found

create_cmd=(
  kubectl -n "$NAMESPACE" create secret docker-registry "$SECRET_NAME"
  --docker-server="$SERVER"
  --docker-username="$USERNAME"
  --docker-password="$PASSWORD"
)
if [ -n "$EMAIL" ]; then
  create_cmd+=(--docker-email="$EMAIL")
fi

"${create_cmd[@]}"

echo "Created/updated image pull secret '$SECRET_NAME' in namespace '$NAMESPACE' (provider=$PROVIDER, server=$SERVER)"
