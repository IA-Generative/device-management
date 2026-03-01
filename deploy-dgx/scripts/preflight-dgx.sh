#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/lib-dgx.sh"

SETTINGS_FILE="${1:-$SETTINGS_FILE}"

bash "$ROOT_DIR/deploy-dgx/scripts/init-secrets-from-example.sh"

errors=0
warnings=0

err() { echo "ERROR: $*" >&2; errors=$((errors+1)); }
warn() { echo "WARN:  $*" >&2; warnings=$((warnings+1)); }
info() { echo "INFO:  $*"; }

check_required_yaml_key() {
  local file="$1"
  local key="$2"
  local v
  v="$(read_yaml_key "$file" "$key")"
  if [ -z "$v" ]; then
    err "Missing value for '$key' in $file"
  fi
}

check_non_placeholder() {
  local file="$1"
  local key="$2"
  local v
  v="$(read_yaml_key "$file" "$key")"
  if [ -z "$v" ] || [ "$v" = "$key" ]; then
    err "Placeholder/empty value for '$key' in $file"
  fi
}

check_not_empty_or_warn() {
  local file="$1"
  local key="$2"
  local v
  v="$(read_yaml_key "$file" "$key")"
  if [ -z "$v" ]; then
    warn "Empty value for '$key' in $file"
  fi
}

echo "== Preflight DGX =="
echo "Settings: $SETTINGS_FILE"
echo "Secret:   $DEVICE_SECRET_FILE"
echo

require_cmd awk
require_cmd kubectl
confirm_kubectl_context

if [ ! -f "$SETTINGS_FILE" ]; then
  err "Missing $SETTINGS_FILE"
fi
if [ ! -f "$DEVICE_SECRET_FILE" ]; then
  err "Missing $DEVICE_SECRET_FILE"
fi
if [ ! -f "$REGISTRY_SECRET_FILE" ]; then
  err "Missing $REGISTRY_SECRET_FILE"
fi

NAMESPACE="$(read_yaml_key "$SETTINGS_FILE" "namespace")"
GATEWAY_NAME="$(read_yaml_key "$SETTINGS_FILE" "gateway_name")"
GATEWAY_NAMESPACE="$(read_yaml_key "$SETTINGS_FILE" "gateway_namespace")"
HOSTNAME="$(read_yaml_key "$SETTINGS_FILE" "hostname")"
DM_STORE_ENROLL_S3="$(read_yaml_key "$SETTINGS_FILE" "dm_store_enroll_s3")"

check_required_yaml_key "$SETTINGS_FILE" namespace
check_required_yaml_key "$SETTINGS_FILE" gateway_name
check_required_yaml_key "$SETTINGS_FILE" gateway_namespace
check_required_yaml_key "$SETTINGS_FILE" hostname
check_required_yaml_key "$SETTINGS_FILE" app_path_prefix
check_required_yaml_key "$SETTINGS_FILE" adminer_path_prefix
check_required_yaml_key "$SETTINGS_FILE" public_base_url

check_non_placeholder "$DEVICE_SECRET_FILE" KEYCLOAK_CLIENT_ID
check_not_empty_or_warn "$DEVICE_SECRET_FILE" KEYCLOAK_REDIRECT_URI
check_not_empty_or_warn "$DEVICE_SECRET_FILE" KEYCLOAK_ALLOWED_REDIRECT_URI

if [ "$(printf "%s" "$DM_STORE_ENROLL_S3" | tr '[:upper:]' '[:lower:]')" = "true" ]; then
  check_non_placeholder "$DEVICE_SECRET_FILE" DM_S3_BUCKET
  check_not_empty_or_warn "$DEVICE_SECRET_FILE" DM_S3_ENDPOINT_URL
  check_non_placeholder "$DEVICE_SECRET_FILE" AWS_ACCESS_KEY_ID
  check_non_placeholder "$DEVICE_SECRET_FILE" AWS_SECRET_ACCESS_KEY
fi

docker_config_json="$(read_yaml_key "$REGISTRY_SECRET_FILE" ".dockerconfigjson")"
if [ -z "$docker_config_json" ] || [ "$docker_config_json" = "{}" ]; then
  warn "Registry secret appears empty in $REGISTRY_SECRET_FILE"
fi

echo
echo "== Environnement 1: poste VS Code / fichiers =="
current_image="$(current_image_ref)"
info "Image manifest: $current_image"
info "Namespace cible: $NAMESPACE"
info "Host cible: $HOSTNAME"

echo
echo "== Environnement 2: cluster DGX =="
if ! kubectl version --client >/dev/null 2>&1; then
  err "kubectl client unavailable"
fi

if ! kubectl get --raw='/readyz' >/dev/null 2>&1; then
  err "Cluster not reachable with current kube context"
else
  info "Cluster reachable"
fi

gateway_resources="$(kubectl api-resources --api-group=gateway.networking.k8s.io -o name 2>/dev/null || true)"
if printf "%s\n" "$gateway_resources" | grep -E -q '^httproutes(\.gateway\.networking\.k8s\.io)?$'; then
  info "Gateway API (HTTPRoute) detected"
elif kubectl explain httproute >/dev/null 2>&1; then
  info "Gateway API (HTTPRoute) detected"
else
  err "HTTPRoute resource not found (Gateway API missing)"
fi

if ! kubectl -n "$GATEWAY_NAMESPACE" get gateway "$GATEWAY_NAME" >/dev/null 2>&1; then
  warn "Gateway '$GATEWAY_NAME' not found in namespace '$GATEWAY_NAMESPACE'"
else
  info "Gateway '$GATEWAY_NAME' found"
fi

if ! kubectl get storageclass >/dev/null 2>&1; then
  warn "Cannot list StorageClass (PVC may fail to bind)"
else
  info "StorageClass list available"
fi

echo
echo "== Resultat preflight =="
echo "Errors:   $errors"
echo "Warnings: $warnings"

if [ "$errors" -gt 0 ]; then
  exit 1
fi

exit 0
