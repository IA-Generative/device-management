#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: missing command: $1" >&2
    exit 1
  }
}

profile_to_overlay() {
  case "${1:-}" in
    local|scaleway|dgx) echo "$ROOT_DIR/deploy/k8s/overlays/$1" ;;
    *)
      echo "ERROR: profile must be one of: local|scaleway|dgx" >&2
      exit 1
      ;;
  esac
}

profile_base_url() {
  case "${1:-}" in
    local) echo "http://bootstrap.home" ;;
    scaleway) echo "https://bootstrap.fake-domain.name" ;;
    dgx) echo "https://onyxia.gpu.minint.fr/bootstrap" ;;
    *) echo "" ;;
  esac
}

profile_scheme() {
  case "${1:-}" in
    local) echo "http" ;;
    scaleway|dgx) echo "https" ;;
    *) echo "" ;;
  esac
}
