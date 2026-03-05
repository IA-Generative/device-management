#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PLUGIN_DIR="${PLUGIN_DIR:-$ROOT_DIR/../AssistantMiraiLibreOffice}"

echo "== Device Management validations =="
"$ROOT_DIR/infra-minimal/validate-all.sh"
"$ROOT_DIR/deploy-dgx/validate-all.sh"

echo
echo "== Kubernetes manifest dry-run =="
kubectl apply --dry-run=client -f "$ROOT_DIR/infra-minimal/bootstrap-app.yaml" >/dev/null
kubectl apply --dry-run=client -k "$ROOT_DIR/deploy-dgx" >/dev/null
echo "Kubernetes dry-run OK"

if [ "${SKIP_PLUGIN_TESTS:-0}" = "1" ]; then
  echo
  echo "Plugin tests skipped (SKIP_PLUGIN_TESTS=1)."
  exit 0
fi

if [ -x "$PLUGIN_DIR/scripts/03-test-local.sh" ]; then
  echo
  echo "== Plugin validations =="
  "$PLUGIN_DIR/scripts/03-test-local.sh"
else
  echo
  echo "Plugin test script not found at: $PLUGIN_DIR/scripts/03-test-local.sh"
  echo "Set PLUGIN_DIR=<path> or run plugin tests manually."
fi
