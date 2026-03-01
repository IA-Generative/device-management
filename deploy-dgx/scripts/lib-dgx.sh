#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SETTINGS_FILE="${SETTINGS_FILE:-$ROOT_DIR/deploy-dgx/settings.yaml}"
DEVICE_SECRET_FILE="${DEVICE_SECRET_FILE:-$ROOT_DIR/deploy-dgx/secrets/10-device-management-secret.yaml}"
REGISTRY_SECRET_FILE="${REGISTRY_SECRET_FILE:-$ROOT_DIR/deploy-dgx/secrets/20-registry-secret.yaml}"
APP_DEPLOYMENT_FILE="${APP_DEPLOYMENT_FILE:-$ROOT_DIR/deploy-dgx/manifests/20-device-management-deployment.yaml}"
REGISTRY_ENV_FILE="${REGISTRY_ENV_FILE:-$ROOT_DIR/deploy-dgx/.env.registry}"

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "ERROR: missing command '$cmd'" >&2
    exit 1
  fi
}

read_yaml_key() {
  local file="$1"
  local key="$2"
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
  ' "$file"
}

set_yaml_key() {
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

set_yaml_block_key() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$value" '
    function lead_spaces(s,   i, c) {
      for (i = 1; i <= length(s); i++) {
        c = substr(s, i, 1)
        if (c != " ") return i - 1
      }
      return length(s)
    }
    BEGIN {
      re = "^[[:space:]]*" k ":[[:space:]]*"
      skip_old_block = 0
      key_indent_len = -1
    }
    {
      if (skip_old_block) {
        if ($0 ~ /^[[:space:]]*$/) {
          next
        }
        cur_indent_len = lead_spaces($0)
        if (cur_indent_len <= key_indent_len) {
          skip_old_block = 0
        } else {
          next
        }
      }
      if ($0 ~ re) {
        key_indent_len = lead_spaces($0)
        indent = substr($0, 1, key_indent_len)
        print indent k ": |"
        n = split(v, lines, /\n/)
        for (i = 1; i <= n; i++) {
          print indent "  " lines[i]
        }
        skip_old_block = 1
        next
      }
      print
    }
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
}

prompt_value() {
  local label="$1"
  local current="$2"
  local out
  read -r -p "$label [$current]: " out
  if [ -z "$out" ]; then
    printf "%s" "$current"
  else
    printf "%s" "$out"
  fi
}

prompt_yes_no() {
  local label="$1"
  local default="${2:-y}"
  local current_prompt="y/N"
  if [ "$default" = "y" ]; then
    current_prompt="Y/n"
  fi
  local ans
  read -r -p "$label ($current_prompt): " ans
  if [ -z "$ans" ]; then
    ans="$default"
  fi
  case "$(printf "%s" "$ans" | tr '[:upper:]' '[:lower:]')" in
    y|yes) return 0 ;;
    *) return 1 ;;
  esac
}

ensure_registry_env_file() {
  local file="${1:-$REGISTRY_ENV_FILE}"
  if [ -f "$file" ]; then
    return 0
  fi

  mkdir -p "$(dirname "$file")"
  cat > "$file" <<'EOF'
# DGX registry credentials (stored on jump host, not committed)
# provider: dockerhub | scaleway | custom
REGISTRY_PROVIDER='dockerhub'
REGISTRY_NAMESPACE='bootstrap'
REGISTRY_SECRET_NAME='regcred'
REGISTRY_SERVER='https://index.docker.io/v1/'
REGISTRY_USERNAME=''
REGISTRY_PASSWORD=''
REGISTRY_EMAIL=''
EOF
  chmod 600 "$file" 2>/dev/null || true
}

_escape_single_quotes() {
  local value="$1"
  printf "%s" "${value//\'/\'\\\'\'}"
}

set_env_file_key() {
  local file="$1"
  local key="$2"
  local value="$3"
  local escaped line tmp
  escaped="$(_escape_single_quotes "$value")"
  line="${key}='${escaped}'"
  tmp="$(mktemp)"

  if [ -f "$file" ]; then
    awk -v k="$key" -v l="$line" '
      BEGIN { done = 0; re = "^" k "=" }
      {
        if ($0 ~ re) {
          if (!done) {
            print l
            done = 1
          }
          next
        }
        print
      }
      END {
        if (!done) {
          print l
        }
      }
    ' "$file" > "$tmp"
  else
    printf "%s\n" "$line" > "$tmp"
  fi

  mv "$tmp" "$file"
}

set_registry_env_key() {
  local key="$1"
  local value="$2"
  ensure_registry_env_file "$REGISTRY_ENV_FILE"
  set_env_file_key "$REGISTRY_ENV_FILE" "$key" "$value"
}

load_registry_env() {
  local file="${1:-$REGISTRY_ENV_FILE}"
  if [ ! -f "$file" ]; then
    return 0
  fi
  # shellcheck disable=SC1090
  set -a
  . "$file"
  set +a
}

confirm_kubectl_context() {
  require_cmd kubectl

  if [ "${DGX_SKIP_CONTEXT_CONFIRM:-0}" = "1" ]; then
    return 0
  fi
  if [ "${DGX_CONTEXT_CONFIRMED:-0}" = "1" ]; then
    return 0
  fi

  local current_context cluster_name server_url default_ns
  current_context="$(kubectl config current-context 2>/dev/null || true)"
  cluster_name="$(kubectl config view --minify -o jsonpath='{.contexts[0].context.cluster}' 2>/dev/null || true)"
  server_url="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || true)"
  default_ns="$(kubectl config view --minify -o jsonpath='{.contexts[0].context.namespace}' 2>/dev/null || true)"
  [ -z "$default_ns" ] && default_ns="<none>"
  [ -z "$current_context" ] && current_context="<unknown>"
  [ -z "$cluster_name" ] && cluster_name="<unknown>"
  [ -z "$server_url" ] && server_url="<unknown>"

  echo "Kubectl context guard:"
  echo "  current-context: $current_context"
  echo "  cluster:         $cluster_name"
  echo "  server:          $server_url"
  echo "  default-namespace: $default_ns"

  if [ -n "${DGX_EXPECTED_CONTEXT:-}" ] && [ "$current_context" != "$DGX_EXPECTED_CONTEXT" ]; then
    echo "WARN: expected context '$DGX_EXPECTED_CONTEXT' but got '$current_context'" >&2
  fi

  if prompt_yes_no "Continuer avec ce contexte kubectl ?" "n"; then
    export DGX_CONTEXT_CONFIRMED=1
    return 0
  fi

  echo "Arret: contexte kubectl non confirme." >&2
  exit 1
}

namespace_from_settings() {
  local settings_file="${1:-$SETTINGS_FILE}"
  local ns
  ns="$(read_yaml_key "$settings_file" "namespace")"
  if [ -z "$ns" ]; then
    echo "ERROR: missing 'namespace' in $settings_file" >&2
    exit 1
  fi
  printf "%s" "$ns"
}

reset_namespace() {
  local ns="$1"
  if [ -z "$ns" ]; then
    echo "ERROR: reset_namespace requires a namespace name" >&2
    exit 1
  fi

  echo
  echo "ATTENTION: cette action va supprimer COMPLETEMENT le namespace '$ns'"
  echo "Toutes les ressources namespace-scoped seront perdues."
  local typed
  read -r -p "Tape exactement '$ns' pour confirmer: " typed
  if [ "$typed" != "$ns" ]; then
    echo "Confirmation invalide, reset annule."
    return 1
  fi

  kubectl delete namespace "$ns" --ignore-not-found --wait=true
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
  echo "Namespace '$ns' recree."
}

current_image_ref() {
  awk '/^[[:space:]]*image:[[:space:]]*/ {print $2; exit}' "$APP_DEPLOYMENT_FILE"
}

current_image_tag() {
  local image
  image="$(current_image_ref)"
  echo "${image##*:}"
}

set_image_tag() {
  local new_tag="$1"
  local current image_no_tag
  current="$(current_image_ref)"
  image_no_tag="${current%:*}"
  local new_image="${image_no_tag}:${new_tag}"
  local tmp
  tmp="$(mktemp)"
  awk -v new_image="$new_image" '
    BEGIN { done = 0 }
    {
      if (!done && $1 == "image:") {
        indent = ""
        if (match($0, /[^ ]/) > 1) {
          indent = substr($0, 1, RSTART - 1)
        }
        print indent "image: " new_image
        done = 1
        next
      }
      print
    }
  ' "$APP_DEPLOYMENT_FILE" > "$tmp"
  mv "$tmp" "$APP_DEPLOYMENT_FILE"
}

update_registry_secret_from_scw_key() {
  local scw_secret_key_raw="$1"
  local scw_secret_key
  scw_secret_key="$(printf "%s" "$scw_secret_key_raw" | tr -d '\r\n')"
  if [ -z "$scw_secret_key" ]; then
    echo "ERROR: SCW secret key is empty after trimming line breaks." >&2
    return 1
  fi
  case "$scw_secret_key" in
    *[[:space:]]*)
      echo "ERROR: SCW secret key contains whitespace; check copy/paste." >&2
      return 1
      ;;
  esac
  if ! printf "%s" "$scw_secret_key" | grep -Eq '^[A-Za-z0-9._-]+$'; then
    echo "ERROR: SCW secret key contains invalid characters; expected plain token (ex: uuid)." >&2
    return 1
  fi
  local registry="rg.fr-par.scw.cloud/funcscwnspricelessmontalcinhiacgnzi"
  local auth
  auth="$(printf "nologin:%s" "$scw_secret_key" | base64 | tr -d '\r\n')"
  local docker_json
  docker_json="$(cat <<EOF
{"auths":{"$registry":{"username":"nologin","password":"$scw_secret_key","auth":"$auth"}}}
EOF
)"
  set_yaml_block_key "$REGISTRY_SECRET_FILE" ".dockerconfigjson" "$docker_json"
}
