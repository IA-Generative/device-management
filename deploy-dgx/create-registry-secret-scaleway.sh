#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$ROOT_DIR/deploy-dgx/scripts/lib-dgx.sh"

SETTINGS_FILE="${1:-$ROOT_DIR/deploy-dgx/settings.yaml}"

ensure_registry_env_file "$REGISTRY_ENV_FILE"

if [ -n "${SCW_SECRET_KEY:-}" ]; then
  set_registry_env_key REGISTRY_PASSWORD "$SCW_SECRET_KEY"
fi
set_registry_env_key REGISTRY_PROVIDER "scaleway"
set_registry_env_key REGISTRY_SECRET_NAME "regcred"
set_registry_env_key REGISTRY_SERVER "rg.fr-par.scw.cloud/funcscwnspricelessmontalcinhiacgnzi"
set_registry_env_key REGISTRY_USERNAME "nologin"
set_registry_env_key REGISTRY_EMAIL ""

exec "$ROOT_DIR/deploy-dgx/create-registry-secret.sh" "$SETTINGS_FILE"
