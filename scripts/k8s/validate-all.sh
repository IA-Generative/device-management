#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

for profile in local scaleway dgx; do
  echo "\n=== Validate profile: $profile ==="
  "$SCRIPT_DIR/render.sh" "$profile"
  kubectl kustomize "$(cd "$SCRIPT_DIR/../.." && pwd)/deploy/k8s/overlays/$profile" >/dev/null
  echo "OK: $profile"
done

echo "\nAll profiles validated (offline kustomize)."
