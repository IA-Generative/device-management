#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$ROOT_DIR/deploy-dgx/scripts/lib-dgx.sh"

SETTINGS_FILE="$ROOT_DIR/deploy-dgx/settings.yaml"
RESET_NAMESPACE=0

bash "$ROOT_DIR/deploy-dgx/scripts/init-secrets-from-example.sh"

usage() {
  cat <<'EOF'
Usage:
  ./deploy-dgx/deploy-full-dgx.sh [settings.yaml] [--reset-namespace]
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --reset-namespace)
      RESET_NAMESPACE=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      SETTINGS_FILE="$1"
      ;;
  esac
  shift
done

confirm_kubectl_context
bash "$ROOT_DIR/deploy-dgx/scripts/render-from-settings.sh" "$SETTINGS_FILE"
NAMESPACE="$(namespace_from_settings "$SETTINGS_FILE")"

if [ "$RESET_NAMESPACE" -eq 1 ]; then
  reset_namespace "$NAMESPACE"
fi

kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/00-namespace.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/secrets/all-secrets.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/10-configmap-device-management.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/18-device-management-content-pvc.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/19-device-management-enroll-pvc.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/20-device-management-deployment.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/21-device-management-service.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/22-httproute.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/29-postgres-pvc.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/30-postgres-deployment.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/31-postgres-service.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/40-adminer-deployment.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/41-adminer-service.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/42-filebrowser-db-pvc.yaml"
kubectl delete -f "$ROOT_DIR/deploy-dgx/manifests/51-filebrowser-users-job.yaml" --ignore-not-found
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/51-filebrowser-users-job.yaml"
if [ -n "$NAMESPACE" ]; then
  kubectl -n "$NAMESPACE" wait --for=condition=complete --timeout=180s job/filebrowser-users-init || true
fi
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/52-filebrowser-deployment.yaml"
kubectl apply -f "$ROOT_DIR/deploy-dgx/manifests/53-filebrowser-service.yaml"

if [ -n "$NAMESPACE" ]; then
  kubectl -n "$NAMESPACE" rollout status deployment/device-management --timeout=180s || true
  kubectl -n "$NAMESPACE" rollout status deployment/filebrowser --timeout=180s || true
fi

if [ "${DGX_SKIP_SMOKE_TEST:-0}" = "1" ]; then
  echo "DGX_SKIP_SMOKE_TEST=1 -> skipping post-deploy smoke tests."
else
  bash "$ROOT_DIR/deploy-dgx/scripts/smoke-test-dgx.sh" "$SETTINGS_FILE"
fi

echo "Full DGX deployment applied/updated."
