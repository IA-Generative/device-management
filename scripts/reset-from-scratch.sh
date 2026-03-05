#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ "${FORCE:-0}" != "1" ]; then
  echo "Refusing destructive reset without FORCE=1"
  echo "Usage: FORCE=1 RESET_K8S=1 ./scripts/reset-from-scratch.sh"
  exit 1
fi

echo "== Reset from scratch =="

if command -v docker >/dev/null 2>&1; then
  echo "Stopping compose stacks..."
  docker compose -f "$ROOT_DIR/infra-minimal/docker-compose.yml" down -v --remove-orphans || true
  docker compose -f "$ROOT_DIR/deploy-dgx/docker-compose.yml" down -v --remove-orphans || true
fi

echo "Removing local data directories..."
rm -rf "$ROOT_DIR/infra-minimal/data" || true
rm -rf "$ROOT_DIR/deploy-dgx/data" || true

if [ "${RESET_K8S:-0}" = "1" ] && command -v kubectl >/dev/null 2>&1; then
  echo "Deleting Kubernetes namespace bootstrap..."
  kubectl delete namespace bootstrap --ignore-not-found=true || true
fi

echo "Cleaning generated secrets (keeps *-example files)..."
rm -f "$ROOT_DIR/deploy-dgx/secrets/10-device-management-secret.yaml" || true
rm -f "$ROOT_DIR/deploy-dgx/secrets/20-registry-secret.yaml" || true
rm -f "$ROOT_DIR/deploy-dgx/secrets/30-filebrowser-users-secret.yaml" || true
rm -f "$ROOT_DIR/deploy-dgx/secrets/all-secrets.yaml" || true

echo "Reset complete."
echo "Next steps:"
echo "  1) cp deploy-dgx/secrets/10-device-management-secret-example.yaml deploy-dgx/secrets/10-device-management-secret.yaml"
echo "  2) ./deploy-dgx/scripts/configure-interactive-dgx.sh"
echo "  3) ./deploy-dgx/deploy-full-dgx.sh"
