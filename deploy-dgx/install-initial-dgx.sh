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
  ./deploy-dgx/install-initial-dgx.sh [settings.yaml] [--reset-namespace]
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

echo "== Installation initiale DGX =="
echo "Settings file: $SETTINGS_FILE"
echo

if [ ! -f "$SETTINGS_FILE" ]; then
  echo "ERROR: missing settings file $SETTINGS_FILE" >&2
  exit 1
fi

confirm_kubectl_context

echo
echo "Etape 1/5: configuration des variables de deploiement"
read -r -p "Lancer l'assistant de configuration interactive ? (Y/n): " ans_cfg
ans_cfg="${ans_cfg:-Y}"
if [[ "$ans_cfg" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]; then
  SETTINGS_FILE="$SETTINGS_FILE" bash "$ROOT_DIR/deploy-dgx/scripts/configure-interactive-dgx.sh"
else
  bash "$ROOT_DIR/deploy-dgx/scripts/render-from-settings.sh" "$SETTINGS_FILE"
fi

echo
echo "Etape 2/5: preflight des 2 environnements"
echo "Lancement du preflight des 2 environnements..."
if SETTINGS_FILE="$SETTINGS_FILE" bash "$ROOT_DIR/deploy-dgx/scripts/preflight-dgx.sh"; then
  echo "Preflight OK."
else
  echo "Preflight en erreur."
  read -r -p "Continuer quand meme le deploiement initial ? (y/N): " ans_force
  ans_force="${ans_force:-N}"
  if [[ ! "$ans_force" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]; then
    echo "Arret."
    exit 1
  fi
fi

echo
echo "Etape 3/5: reset optionnel du namespace cible"
NAMESPACE="$(namespace_from_settings "$SETTINGS_FILE")"
if [ "$RESET_NAMESPACE" -eq 1 ]; then
  reset_namespace "$NAMESPACE"
else
  read -r -p "Supprimer puis recreer le namespace '$NAMESPACE' avant install ? (y/N): " ans_reset
  ans_reset="${ans_reset:-N}"
  if [[ "$ans_reset" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]; then
    reset_namespace "$NAMESPACE"
  fi
fi

echo
echo "Etape 4/5: configuration du secret registry (machine de rebond -> cluster DGX)"
read -r -p "Configurer/mettre a jour les credentials registry maintenant ? (Y/n): " ans_registry
ans_registry="${ans_registry:-Y}"
if [[ "$ans_registry" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]; then
  SETTINGS_FILE="$SETTINGS_FILE" bash "$ROOT_DIR/deploy-dgx/scripts/configure-registry-dgx.sh" --apply
else
  echo "Secrets registry non modifies."
fi

echo
echo "Etape 5/5: deploiement complet"
read -r -p "Demarrer le deploiement complet maintenant ? (Y/n): " ans_deploy
ans_deploy="${ans_deploy:-Y}"
if [[ "$ans_deploy" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]; then
  bash "$ROOT_DIR/deploy-dgx/deploy-full-dgx.sh" "$SETTINGS_FILE"
  echo "Installation initiale terminee."
else
  echo "Deploiement annule."
fi
