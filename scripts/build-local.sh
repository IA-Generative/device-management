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
  -f "$ROOT_DIR/deploy/docker/Dockerfile" \
  "$ROOT_DIR"

echo "Done. Run with: docker compose -f deploy/docker/docker-compose.yml up -d"
