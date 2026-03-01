#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
SETTINGS_FILE="${1:-$ROOT_DIR/deploy-dgx/settings.yaml}"

NAMESPACE_MANIFEST="$ROOT_DIR/deploy-dgx/manifests/00-namespace.yaml"
HTTPROUTE_MANIFEST="$ROOT_DIR/deploy-dgx/manifests/22-httproute.yaml"
DEVICE_SECRET="$ROOT_DIR/deploy-dgx/secrets/10-device-management-secret.yaml"
REGISTRY_SECRET="$ROOT_DIR/deploy-dgx/secrets/20-registry-secret.yaml"
FILEBROWSER_USERS_SECRET="$ROOT_DIR/deploy-dgx/secrets/30-filebrowser-users-secret.yaml"
ALL_SECRETS="$ROOT_DIR/deploy-dgx/secrets/all-secrets.yaml"

if [ ! -f "$SETTINGS_FILE" ]; then
  echo "Missing settings file: $SETTINGS_FILE" >&2
  exit 1
fi

read_setting() {
  local key="$1"
  awk -v k="$key" '
    /^[[:space:]]*#/ {next}
    /^[[:space:]]*$/ {next}
    $0 ~ "^[[:space:]]*" k ":[[:space:]]*" {
      line=$0
      sub("^[[:space:]]*" k ":[[:space:]]*", "", line)
      sub("[[:space:]]+#.*$", "", line)
      gsub(/^"/, "", line); gsub(/"$/, "", line)
      gsub(/^'\''/, "", line); gsub(/'\''$/, "", line)
      print line
      exit
    }
  ' "$SETTINGS_FILE"
}

require_setting() {
  local key="$1"
  local value
  value="$(read_setting "$key")"
  if [ -z "$value" ]; then
    echo "Missing setting '$key' in $SETTINGS_FILE" >&2
    exit 1
  fi
  printf "%s" "$value"
}

replace_yaml_key() {
  local file="$1"
  local key="$2"
  local value="$3"
  local quoted="${4:-true}"
  local tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$value" -v q="$quoted" '
    BEGIN { re = "^[[:space:]]*" k ":[[:space:]]*" }
    {
      if ($0 ~ re) {
        indent = ""
        if (match($0, /[^ ]/) > 1) {
          indent = substr($0, 1, RSTART - 1)
        }
        if (q == "true") {
          print indent k ": \"" v "\""
        } else {
          print indent k ": " v
        }
        next
      }
      print
    }
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
}

NAMESPACE="$(require_setting namespace)"
GATEWAY_NAME="$(require_setting gateway_name)"
GATEWAY_NAMESPACE="$(require_setting gateway_namespace)"
HOSTNAME="$(require_setting hostname)"
APP_PATH_PREFIX="$(require_setting app_path_prefix)"
ADMINER_PATH_PREFIX="$(require_setting adminer_path_prefix)"
FILEBROWSER_PATH_PREFIX="$(read_setting filebrowser_path_prefix)"
PUBLIC_BASE_URL="$(require_setting public_base_url)"
KEYCLOAK_ISSUER_URL="$(require_setting keycloak_issuer_url)"
KEYCLOAK_REALM="$(require_setting keycloak_realm)"
DM_STORE_ENROLL_S3="$(require_setting dm_store_enroll_s3)"
DM_BINARIES_MODE="$(require_setting dm_binaries_mode)"

APP_PATH_PREFIX="${APP_PATH_PREFIX%/}"
if [ -z "$APP_PATH_PREFIX" ]; then
  APP_PATH_PREFIX="/"
fi
ADMINER_PATH_PREFIX="${ADMINER_PATH_PREFIX%/}"
if [ -z "$ADMINER_PATH_PREFIX" ]; then
  ADMINER_PATH_PREFIX="/adminer"
fi
FILEBROWSER_PATH_PREFIX="${FILEBROWSER_PATH_PREFIX%/}"
if [ -z "$FILEBROWSER_PATH_PREFIX" ]; then
  FILEBROWSER_PATH_PREFIX="/files"
fi
if [ "$APP_PATH_PREFIX" = "/" ]; then
  DM_ENROLL_URL="/enroll"
else
  DM_ENROLL_URL="${APP_PATH_PREFIX}/enroll"
fi

cat > "$NAMESPACE_MANIFEST" <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
EOF

cat > "$HTTPROUTE_MANIFEST" <<EOF
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: device-management-route
  namespace: ${NAMESPACE}
spec:
  hostnames:
    - ${HOSTNAME}
  parentRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: ${GATEWAY_NAME}
      namespace: ${GATEWAY_NAMESPACE}
  rules:
    - backendRefs:
        - group: ""
          kind: Service
          name: device-management
          port: 80
          weight: 1
      matches:
        - path:
            type: PathPrefix
            value: ${APP_PATH_PREFIX}
      filters:
        - type: URLRewrite
          urlRewrite:
            path:
              type: ReplacePrefixMatch
              replacePrefixMatch: /
      timeouts:
        request: 300s
    - backendRefs:
        - group: ""
          kind: Service
          name: adminer
          port: 8080
          weight: 1
      matches:
        - path:
            type: PathPrefix
            value: ${ADMINER_PATH_PREFIX}
      filters:
        - type: URLRewrite
          urlRewrite:
            path:
              type: ReplacePrefixMatch
              replacePrefixMatch: /
      timeouts:
        request: 300s
    - backendRefs:
        - group: ""
          kind: Service
          name: filebrowser
          port: 80
          weight: 1
      matches:
        - path:
            type: PathPrefix
            value: ${FILEBROWSER_PATH_PREFIX}
      filters:
        - type: URLRewrite
          urlRewrite:
            path:
              type: ReplacePrefixMatch
              replacePrefixMatch: /
      timeouts:
        request: 300s
EOF

# Keep all namespace-scoped manifests aligned with settings.yaml
for f in \
  "$ROOT_DIR/deploy-dgx/manifests/18-device-management-content-pvc.yaml" \
  "$ROOT_DIR/deploy-dgx/manifests/19-device-management-enroll-pvc.yaml" \
  "$ROOT_DIR/deploy-dgx/manifests/20-device-management-deployment.yaml" \
  "$ROOT_DIR/deploy-dgx/manifests/21-device-management-service.yaml" \
  "$ROOT_DIR/deploy-dgx/manifests/29-postgres-pvc.yaml" \
  "$ROOT_DIR/deploy-dgx/manifests/30-postgres-deployment.yaml" \
  "$ROOT_DIR/deploy-dgx/manifests/31-postgres-service.yaml" \
  "$ROOT_DIR/deploy-dgx/manifests/40-adminer-deployment.yaml" \
  "$ROOT_DIR/deploy-dgx/manifests/41-adminer-service.yaml" \
  "$ROOT_DIR/deploy-dgx/manifests/42-filebrowser-db-pvc.yaml" \
  "$ROOT_DIR/deploy-dgx/manifests/51-filebrowser-users-job.yaml" \
  "$ROOT_DIR/deploy-dgx/manifests/52-filebrowser-deployment.yaml" \
  "$ROOT_DIR/deploy-dgx/manifests/53-filebrowser-service.yaml" \
  "$ROOT_DIR/deploy-dgx/manifests/10-configmap-device-management.yaml" \
  "$DEVICE_SECRET" \
  "$REGISTRY_SECRET" \
  "$FILEBROWSER_USERS_SECRET"; do
  replace_yaml_key "$f" "namespace" "$NAMESPACE" false
done

replace_yaml_key "$DEVICE_SECRET" "DM_ENROLL_URL" "$DM_ENROLL_URL" true
replace_yaml_key "$DEVICE_SECRET" "DM_STORE_ENROLL_S3" "$DM_STORE_ENROLL_S3" true
replace_yaml_key "$DEVICE_SECRET" "DM_BINARIES_MODE" "$DM_BINARIES_MODE" true
replace_yaml_key "$DEVICE_SECRET" "KEYCLOAK_ISSUER_URL" "$KEYCLOAK_ISSUER_URL" true
replace_yaml_key "$DEVICE_SECRET" "KEYCLOAK_REALM" "$KEYCLOAK_REALM" true
replace_yaml_key "$DEVICE_SECRET" "PUBLIC_BASE_URL" "$PUBLIC_BASE_URL" true

cat "$DEVICE_SECRET" > "$ALL_SECRETS"
printf "\n---\n" >> "$ALL_SECRETS"
cat "$FILEBROWSER_USERS_SECRET" >> "$ALL_SECRETS"

echo "Rendered manifests/secrets from $SETTINGS_FILE"
