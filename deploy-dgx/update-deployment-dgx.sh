#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$ROOT_DIR/deploy-dgx/scripts/lib-dgx.sh"

SETTINGS_FILE="${1:-$SETTINGS_FILE}"
NAMESPACE="$(read_yaml_key "$SETTINGS_FILE" "namespace")"
current_tag="$(current_image_tag)"
current_image="$(current_image_ref)"

confirm_kubectl_context

echo "== Update deployment DGX =="
echo "Image actuelle: $current_image"
echo

read -r -p "Nouveau tag image (laisser vide = $current_tag): " new_tag
new_tag="${new_tag:-$current_tag}"

if [ "$new_tag" != "$current_tag" ]; then
  echo "Changement de tag: $current_tag -> $new_tag"
  set_image_tag "$new_tag"
  echo "Manifest mis a jour: $APP_DEPLOYMENT_FILE"
  echo
  read -r -p "Relancer un deploiement complet maintenant ? (Y/n): " ans_full
  ans_full="${ans_full:-Y}"
  if [[ "$ans_full" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]; then
    bash "$ROOT_DIR/deploy-dgx/deploy-full-dgx.sh" "$SETTINGS_FILE"
  else
    echo "Deploiement complet saute. Tu peux lancer:"
    echo "  ./deploy-dgx/deploy-full-dgx.sh"
  fi
  exit 0
fi

echo "Tag identique ($current_tag)."
read -r -p "Forcer un pull image en redemarrant le deployment ? (Y/n): " ans_force
ans_force="${ans_force:-Y}"
if [[ ! "$ans_force" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]; then
  echo "Aucune action effectuee."
  exit 0
fi

kubectl apply -f "$APP_DEPLOYMENT_FILE"
kubectl -n "$NAMESPACE" rollout restart deployment/device-management
kubectl -n "$NAMESPACE" rollout status deployment/device-management --timeout=180s
echo "Redemarrage force termine."
