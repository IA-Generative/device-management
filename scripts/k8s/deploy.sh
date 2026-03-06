#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

PROFILE="${1:-}"
if [ -z "$PROFILE" ]; then
  echo "Usage: $0 <local|scaleway|dgx>" >&2
  exit 1
fi

OVERLAY="$(profile_to_overlay "$PROFILE")"

require_cmd kubectl
"$SCRIPT_DIR/render.sh" "$PROFILE"

NAMESPACE="${REGISTRY_NAMESPACE:-bootstrap}"
SECRET_NAME="${REGISTRY_SECRET_NAME:-regcred}"
if ! kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" >/dev/null 2>&1; then
  echo "WARN: image pull secret '$SECRET_NAME' not found in namespace '$NAMESPACE'."
  echo "      create it with: ./scripts/k8s/create-registry-secret.sh $PROFILE"
fi

echo "\nApplying overlay: $OVERLAY"
kubectl apply -k "$OVERLAY"

echo "\nDeployment submitted for profile '$PROFILE'."
