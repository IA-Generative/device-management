#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/lib-dgx.sh"

SETTINGS_FILE="${1:-$SETTINGS_FILE}"

bash "$ROOT_DIR/deploy-dgx/scripts/init-secrets-from-example.sh"

if [ ! -f "$SETTINGS_FILE" ]; then
  echo "Missing $SETTINGS_FILE" >&2
  exit 1
fi
if [ ! -f "$DEVICE_SECRET_FILE" ]; then
  echo "Missing $DEVICE_SECRET_FILE" >&2
  exit 1
fi

echo "Configuration interactive DGX"
echo "Fichier settings: $SETTINGS_FILE"
echo "Secret applicatif: $DEVICE_SECRET_FILE"
echo

update_setting() {
  local key="$1"
  local label="$2"
  local quoted="${3:-false}"
  local cur newv
  cur="$(read_yaml_key "$SETTINGS_FILE" "$key")"
  newv="$(prompt_value "$label" "$cur")"
  set_yaml_key "$SETTINGS_FILE" "$key" "$newv" "$quoted"
}

update_secret_key() {
  local key="$1"
  local label="$2"
  local cur newv
  cur="$(read_yaml_key "$DEVICE_SECRET_FILE" "$key")"
  newv="$(prompt_value "$label" "$cur")"
  set_yaml_key "$DEVICE_SECRET_FILE" "$key" "$newv" true
}

echo "1) Settings reseau / routage"
update_setting namespace "Namespace kubernetes"
update_setting gateway_name "Gateway name (Gateway API)"
update_setting gateway_namespace "Gateway namespace"
update_setting hostname "Hostname expose"
update_setting app_path_prefix "Prefix app python (/bootstrap)"
update_setting adminer_path_prefix "Prefix adminer (/adminer)"
update_setting filebrowser_path_prefix "Prefix filebrowser (/files)"
update_setting public_base_url "PUBLIC_BASE_URL complet"

echo
echo "2) Settings applicatifs"
update_setting keycloak_issuer_url "Keycloak issuer URL"
update_setting keycloak_realm "Keycloak realm"
update_setting dm_store_enroll_s3 "DM_STORE_ENROLL_S3 (true/false)" true
update_setting dm_binaries_mode "DM_BINARIES_MODE (proxy/presign/local)"

echo
echo "3) Secrets applicatifs"
update_secret_key KEYCLOAK_CLIENT_ID "KEYCLOAK_CLIENT_ID"
update_secret_key KEYCLOAK_REDIRECT_URI "KEYCLOAK_REDIRECT_URI"
update_secret_key KEYCLOAK_ALLOWED_REDIRECT_URI "KEYCLOAK_ALLOWED_REDIRECT_URI"
update_secret_key DM_S3_BUCKET "DM_S3_BUCKET"
update_secret_key DM_S3_ENDPOINT_URL "DM_S3_ENDPOINT_URL"
update_secret_key AWS_REGION "AWS_REGION"
update_secret_key AWS_ACCESS_KEY_ID "AWS_ACCESS_KEY_ID"
update_secret_key AWS_SECRET_ACCESS_KEY "AWS_SECRET_ACCESS_KEY"

if prompt_yes_no "Renseigner aussi AWS_SESSION_TOKEN ?" "n"; then
  update_secret_key AWS_SESSION_TOKEN "AWS_SESSION_TOKEN"
fi

if prompt_yes_no "Configurer aussi les credentials registry (DockerHub/Scaleway/custom) ?" "n"; then
  SETTINGS_FILE="$SETTINGS_FILE" bash "$ROOT_DIR/deploy-dgx/scripts/configure-registry-dgx.sh"
fi

"$ROOT_DIR/deploy-dgx/scripts/render-from-settings.sh" "$SETTINGS_FILE"
echo
echo "Configuration terminee + manifests regeneres."
