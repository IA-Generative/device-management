#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/lib-dgx.sh"

SETTINGS_FILE="$ROOT_DIR/deploy-dgx/settings.yaml"
APPLY_SECRET=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --apply)
      APPLY_SECRET=1
      ;;
    -h|--help)
      cat <<'EOF'
Usage:
  ./deploy-dgx/scripts/configure-registry-dgx.sh [settings.yaml] [--apply]
EOF
      exit 0
      ;;
    *)
      SETTINGS_FILE="$1"
      ;;
  esac
  shift
done

if [ ! -f "$SETTINGS_FILE" ]; then
  echo "ERROR: missing settings file $SETTINGS_FILE" >&2
  exit 1
fi

require_cmd kubectl
confirm_kubectl_context

ensure_registry_env_file "$REGISTRY_ENV_FILE"
load_registry_env "$REGISTRY_ENV_FILE"

DEFAULT_NAMESPACE="$(namespace_from_settings "$SETTINGS_FILE")"
CURRENT_PROVIDER="${REGISTRY_PROVIDER:-dockerhub}"
CURRENT_PROVIDER="$(printf "%s" "$CURRENT_PROVIDER" | tr '[:upper:]' '[:lower:]')"
[ -z "$CURRENT_PROVIDER" ] && CURRENT_PROVIDER="dockerhub"

echo "Configuration registry (machine de rebond)"
echo "Fichier persistant: $REGISTRY_ENV_FILE"
echo
echo "Provider actuel: $CURRENT_PROVIDER"

read -r -p "Provider registry (dockerhub/scaleway/custom) [$CURRENT_PROVIDER]: " provider
provider="${provider:-$CURRENT_PROVIDER}"
provider="$(printf "%s" "$provider" | tr '[:upper:]' '[:lower:]')"

case "$provider" in
  dockerhub|scaleway|custom) ;;
  *)
    echo "ERROR: provider non supporte '$provider' (dockerhub|scaleway|custom)" >&2
    exit 1
    ;;
esac

current_namespace="${REGISTRY_NAMESPACE:-$DEFAULT_NAMESPACE}"
current_secret_name="${REGISTRY_SECRET_NAME:-regcred}"
namespace="$(prompt_value "Namespace du secret imagePull" "$current_namespace")"
secret_name="$(prompt_value "Nom du secret imagePull" "$current_secret_name")"

set_registry_env_key REGISTRY_PROVIDER "$provider"
set_registry_env_key REGISTRY_NAMESPACE "$namespace"
set_registry_env_key REGISTRY_SECRET_NAME "$secret_name"

if [ "$provider" = "dockerhub" ]; then
  current_server="${REGISTRY_SERVER:-https://index.docker.io/v1/}"
  current_user="${REGISTRY_USERNAME:-}"
  current_email="${REGISTRY_EMAIL:-}"
  current_password="${REGISTRY_PASSWORD:-}"

  server="$(prompt_value "Docker server" "$current_server")"
  user="$(prompt_value "Docker Hub username" "$current_user")"
  email="$(prompt_value "Docker Hub email (optionnel)" "$current_email")"
  read -r -s -p "Docker Hub PAT (laisser vide pour conserver l'existant): " password
  echo
  if [ -z "$password" ]; then
    password="$current_password"
  fi

  if [ -z "$user" ] || [ -z "$password" ]; then
    echo "ERROR: username et PAT Docker Hub obligatoires." >&2
    exit 1
  fi

  set_registry_env_key REGISTRY_SERVER "$server"
  set_registry_env_key REGISTRY_USERNAME "$user"
  set_registry_env_key REGISTRY_PASSWORD "$password"
  set_registry_env_key REGISTRY_EMAIL "$email"
fi

if [ "$provider" = "scaleway" ]; then
  current_server="${REGISTRY_SERVER:-rg.fr-par.scw.cloud/funcscwnspricelessmontalcinhiacgnzi}"
  current_user="${REGISTRY_USERNAME:-nologin}"
  current_password="${REGISTRY_PASSWORD:-}"

  server="$(prompt_value "Registry server Scaleway" "$current_server")"
  user="$(prompt_value "Registry username Scaleway" "$current_user")"
  read -r -s -p "SCW_SECRET_KEY / password registry (laisser vide pour conserver): " password
  echo
  if [ -z "$password" ]; then
    password="$current_password"
  fi

  if [ -z "$password" ]; then
    echo "ERROR: mot de passe registry Scaleway obligatoire." >&2
    exit 1
  fi

  set_registry_env_key REGISTRY_SERVER "$server"
  set_registry_env_key REGISTRY_USERNAME "$user"
  set_registry_env_key REGISTRY_PASSWORD "$password"
  set_registry_env_key REGISTRY_EMAIL ""
fi

if [ "$provider" = "custom" ]; then
  current_server="${REGISTRY_SERVER:-}"
  current_user="${REGISTRY_USERNAME:-}"
  current_email="${REGISTRY_EMAIL:-}"
  current_password="${REGISTRY_PASSWORD:-}"

  server="$(prompt_value "Registry server custom" "$current_server")"
  user="$(prompt_value "Registry username custom" "$current_user")"
  email="$(prompt_value "Registry email custom (optionnel)" "$current_email")"
  read -r -s -p "Registry password custom (laisser vide pour conserver): " password
  echo
  if [ -z "$password" ]; then
    password="$current_password"
  fi

  if [ -z "$server" ] || [ -z "$user" ] || [ -z "$password" ]; then
    echo "ERROR: server, username et password obligatoires pour custom." >&2
    exit 1
  fi

  set_registry_env_key REGISTRY_SERVER "$server"
  set_registry_env_key REGISTRY_USERNAME "$user"
  set_registry_env_key REGISTRY_PASSWORD "$password"
  set_registry_env_key REGISTRY_EMAIL "$email"
fi

echo
echo "Configuration registry enregistree dans $REGISTRY_ENV_FILE"

if [ "$APPLY_SECRET" -eq 1 ]; then
  "$ROOT_DIR/deploy-dgx/create-registry-secret.sh" "$SETTINGS_FILE"
else
  if prompt_yes_no "Appliquer maintenant le secret registry sur le cluster ?" "y"; then
    "$ROOT_DIR/deploy-dgx/create-registry-secret.sh" "$SETTINGS_FILE"
  fi
fi
