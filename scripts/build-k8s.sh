#!/usr/bin/env bash
# Build and push the device-management image for K8S (linux/amd64 + linux/arm64).
# Reads registry from .env.registry.
#
# Usage:
#   ./scripts/build-k8s.sh <tag>              # build + push with given tag
#   ./scripts/build-k8s.sh <tag> --no-push    # build only (loads into local docker)
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TAG="${1:?Usage: $0 <tag> [--no-push]}"
NO_PUSH="${2:-}"

BUILDER_NAME="dm-multiarch"
PLATFORMS="linux/amd64,linux/arm64"

# ---- Load registry config
ENV_REGISTRY="$ROOT_DIR/.env.registry"
if [ ! -f "$ENV_REGISTRY" ]; then
  echo "ERROR: $ENV_REGISTRY not found. Copy .env.registry.example and fill in values." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$ENV_REGISTRY"

REGISTRY="${REGISTRY_SERVER:?REGISTRY_SERVER not set in .env.registry}"
IMAGE="$REGISTRY/device-management"

# ---- Ensure buildx builder exists
if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
  echo "Creating buildx builder: $BUILDER_NAME"
  docker buildx create --name "$BUILDER_NAME" --driver docker-container --use
else
  docker buildx use "$BUILDER_NAME"
fi

echo "== Build K8S multi-arch ($PLATFORMS) =="
echo "Image: $IMAGE:$TAG"

if [ "$NO_PUSH" = "--no-push" ]; then
  docker buildx build \
    --builder "$BUILDER_NAME" \
    --platform "$PLATFORMS" \
    -t "$IMAGE:$TAG" \
    -f "$ROOT_DIR/deploy/docker/Dockerfile" \
    "$ROOT_DIR"
  echo "Built (not pushed). Use without --no-push to push."
else
  docker buildx build \
    --builder "$BUILDER_NAME" \
    --platform "$PLATFORMS" \
    -t "$IMAGE:$TAG" \
    -t "$IMAGE:latest" \
    -f "$ROOT_DIR/deploy/docker/Dockerfile" \
    --push \
    "$ROOT_DIR"
  echo "Pushed: $IMAGE:$TAG + $IMAGE:latest"
fi
