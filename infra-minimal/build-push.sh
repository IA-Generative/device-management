#!/usr/bin/env sh
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"

REGISTRY="rg.fr-par.scw.cloud/funcscwnspricelessmontalcinhiacgnzi"
IMAGE_NAME="device-management"
TAG="${TAG:-latest}"
IMAGE_REF="$REGISTRY/$IMAGE_NAME:$TAG"

if [ -z "${SCW_SECRET_KEY:-}" ]; then
  echo "SCW_SECRET_KEY is required to login to $REGISTRY" >&2
  exit 1
fi

if [ ! -f "$REPO_ROOT/infra-minimal/db-schema.sql" ]; then
  echo "Missing $REPO_ROOT/infra-minimal/db-schema.sql (build context must be repo root)." >&2
  exit 1
fi

if ! echo "$SCW_SECRET_KEY" | docker login "$REGISTRY" -u nologin --password-stdin; then
  echo "Docker login failed. Check that SCW_SECRET_KEY is valid (not expired) and that the registry/namespace is correct." >&2
  echo "Tip: regenerate a new Scaleway API key and retry." >&2
  exit 1
fi

docker build -f "$REPO_ROOT/infra-minimal/Dockerfile" -t "$IMAGE_REF" "$REPO_ROOT"

# Multi-arch build (amd64 + arm64) and push
if ! docker buildx ls >/dev/null 2>&1; then
  echo "docker buildx not available. Please enable Docker Buildx." >&2
  exit 1
fi

BUILDER_NAME="${BUILDER_NAME:-dm-multiarch}"
if ! docker buildx inspect "$BUILDER_NAME" >/dev/null 2>&1; then
  docker buildx create --name "$BUILDER_NAME" --use
else
  docker buildx use "$BUILDER_NAME"
fi

docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -f "$REPO_ROOT/infra-minimal/Dockerfile" \
  -t "$IMAGE_REF" \
  "$REPO_ROOT" \
  --push

echo "Pushed $IMAGE_REF"
