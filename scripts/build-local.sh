#!/usr/bin/env bash
# Build the device-management image for LOCAL docker-compose (arm64 only, fast).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGE_NAME="${DM_IMAGE_NAME:-device-management}"
TAG="${1:-latest}"

echo "== Build local (arm64) =="
echo "Image: $IMAGE_NAME:$TAG"

docker build \
  -t "$IMAGE_NAME:$TAG" \
  --build-arg DM_IMAGE_TAG="$TAG" \
  --build-arg COMMIT="$(git -C "$ROOT_DIR" rev-parse HEAD)" \
  --build-arg CODE_DATE="$(git -C "$ROOT_DIR" log -1 --format=%cI)" \
  --build-arg SOURCE="https://github.com/IA-Generative/device-management" \
  -f "$ROOT_DIR/deploy/docker/Dockerfile" \
  "$ROOT_DIR"

echo "Version portée par l'image :"
docker run --rm --entrypoint cat "$IMAGE_NAME:$TAG" /app/version.json
echo "Done. Run with: docker compose -f deploy/docker/docker-compose.yml up -d"
