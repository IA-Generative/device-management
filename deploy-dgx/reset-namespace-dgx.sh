#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$ROOT_DIR/deploy-dgx/scripts/lib-dgx.sh"

SETTINGS_FILE="${1:-$ROOT_DIR/deploy-dgx/settings.yaml}"

if [ ! -f "$SETTINGS_FILE" ]; then
  echo "ERROR: missing settings file $SETTINGS_FILE" >&2
  exit 1
fi

confirm_kubectl_context
NAMESPACE="$(namespace_from_settings "$SETTINGS_FILE")"
reset_namespace "$NAMESPACE"
