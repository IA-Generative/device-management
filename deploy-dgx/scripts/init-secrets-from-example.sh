#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

copy_if_missing() {
  local dst="$1"
  local src="$2"
  if [ -f "$dst" ]; then
    return 0
  fi
  if [ ! -f "$src" ]; then
    echo "WARN: missing example file $src" >&2
    return 0
  fi
  cp "$src" "$dst"
  chmod 600 "$dst" 2>/dev/null || true
  echo "Initialized $dst from $src"
}

copy_if_missing "$ROOT_DIR/deploy-dgx/.env.registry" "$ROOT_DIR/deploy-dgx/.env.registry.example"
copy_if_missing "$ROOT_DIR/deploy-dgx/secrets/10-device-management-secret.yaml" "$ROOT_DIR/deploy-dgx/secrets/10-device-management-secret-example.yaml"
copy_if_missing "$ROOT_DIR/deploy-dgx/secrets/20-registry-secret.yaml" "$ROOT_DIR/deploy-dgx/secrets/20-registry-secret-example.yaml"
copy_if_missing "$ROOT_DIR/deploy-dgx/secrets/30-filebrowser-users-secret.yaml" "$ROOT_DIR/deploy-dgx/secrets/30-filebrowser-users-secret-example.yaml"
