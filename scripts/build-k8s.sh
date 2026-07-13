#!/usr/bin/env bash
# Build and push the device-management image for K8S (linux/amd64 + linux/arm64).
# Reads registry from .env.registry.
#
# Usage:
#   ./scripts/build-k8s.sh                    # tag = VERSION (racine du repo)
#   ./scripts/build-k8s.sh <tag>              # build + push with given tag
#   ./scripts/build-k8s.sh <tag> --no-push    # build only (loads into local docker)
#
# VERSION est maintenue par device-management-private/deploy/scripts/bump-version.sh
# (propagée depuis le repo de déploiement à chaque bump).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TAG="${1:-}"
NO_PUSH="${2:-}"

# Pas de tag en argument → défaut = fichier VERSION à la racine du repo.
# (--no-push peut alors être passé en premier argument.)
if [ "$TAG" = "--no-push" ]; then TAG=""; NO_PUSH="--no-push"; fi
if [ -z "$TAG" ]; then
  VERSION_FILE="$ROOT_DIR/VERSION"
  TAG="$( { [ -f "$VERSION_FILE" ] && head -1 "$VERSION_FILE" | tr -d '[:space:]'; } || true )"
  if [ -z "$TAG" ]; then
    echo "ERROR: aucun tag fourni et VERSION vide ou absente ($VERSION_FILE)." >&2
    echo "       Usage: $0 [tag] [--no-push] — ou renseigne VERSION" >&2
    echo "       (bump-version.sh du repo device-management-private la met à jour)" >&2
    exit 1
  fi
  echo "Tag par défaut (VERSION): $TAG"
fi

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
