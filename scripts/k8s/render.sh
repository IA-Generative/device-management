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
BASE_URL="$(profile_base_url "$PROFILE")"
SCHEME="$(profile_scheme "$PROFILE")"

require_cmd kubectl

if [ ! -d "$OVERLAY" ]; then
  echo "ERROR: overlay not found: $OVERLAY" >&2
  exit 1
fi

echo "== Render profile: $PROFILE =="
echo "Overlay: $OVERLAY"
echo "Public base URL: $BASE_URL"

echo "\nHTTP/HTTPS impact check:"
if [ "$SCHEME" = "http" ]; then
  echo "- local profile uses HTTP (dev only)."
  echo "- Tokens/credentials can transit in cleartext if network is not trusted."
  echo "- Use only on isolated local network and with short-lived credentials."
else
  echo "- profile uses HTTPS (recommended for production)."
  echo "- Ensure certificate chain is trusted by client machines."
  echo "- Keep TLS verification enabled in production."
fi

echo "\nKustomize validation (offline):"
kubectl kustomize "$OVERLAY" >/dev/null
echo "- OK: manifest set is valid for profile '$PROFILE'."
