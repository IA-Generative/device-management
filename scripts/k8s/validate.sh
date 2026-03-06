#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

PROFILE="${1:-}"
NAMESPACE="${2:-bootstrap}"
if [ -z "$PROFILE" ]; then
  echo "Usage: $0 <local|scaleway|dgx> [namespace]" >&2
  exit 1
fi

OVERLAY="$(profile_to_overlay "$PROFILE")"
BASE_URL="$(profile_base_url "$PROFILE")"

require_cmd kubectl
"$SCRIPT_DIR/render.sh" "$PROFILE"

echo "\nServer-side checks:"
if kubectl cluster-info >/dev/null 2>&1; then
  kubectl apply -k "$OVERLAY" --dry-run=server >/dev/null
  echo "- OK: server dry-run passed"

  kubectl -n "$NAMESPACE" rollout status deploy/postgres --timeout=180s || true
  kubectl -n "$NAMESPACE" rollout status deploy/device-management --timeout=180s || true
  kubectl -n "$NAMESPACE" rollout status deploy/relay-assistant --timeout=180s || true
  kubectl -n "$NAMESPACE" rollout status deploy/telemetry-relay --timeout=180s || true
else
  echo "- SKIP: Kubernetes API unreachable from this machine"
fi

echo "\nExpected public base URL: $BASE_URL"
echo "Validation done for profile '$PROFILE'."
