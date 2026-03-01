#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$ROOT_DIR/deploy-dgx/scripts/lib-dgx.sh"

SETTINGS_FILE="${1:-$ROOT_DIR/deploy-dgx/settings.yaml}"

confirm_kubectl_context
bash "$ROOT_DIR/deploy-dgx/scripts/render-from-settings.sh" "$SETTINGS_FILE"
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/00-namespace.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/secrets/all-secrets.yaml"

echo "Secrets applied from deploy-dgx/secrets/all-secrets.yaml"
