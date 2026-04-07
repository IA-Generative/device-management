#!/usr/bin/env bash
# dumb-deploy.sh — deploiement DGX, une seule commande.
#
# Prerequis : kubectl configure sur le cluster cible.
# Usage :
#   ./dumb-deploy.sh                       # interactif
#   SCW_SECRET_KEY=xxx ./dumb-deploy.sh    # non-interactif
#
# Ce que fait le script :
#   1. Verifie kubectl + cluster joignable
#   2. Cree le namespace bootstrap
#   3. Cree/met a jour le secret regcred (credentials Scaleway registry)
#   4. Applique les manifests
#   5. Attend que les Deployments soient prets
#   6. Affiche l'etat final
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NAMESPACE="bootstrap"
# DockerHub registry (Scaleway was unreachable from DGX, all images mirrored to <DOCKERHUB_NAMESPACE>'s account)
REGISTRY_SERVER="docker.io"
REGISTRY_USERNAME_DEFAULT="<DOCKERHUB_NAMESPACE>"
MANIFESTS="$SCRIPT_DIR/manifests/dgx-all.yaml"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
step()  { echo ""; echo -e "${BLUE}▶ $*${NC}"; }
ok()    { echo -e "  ${GREEN}✓${NC} $*"; }
warn()  { echo -e "  ${YELLOW}⚠${NC} $*"; }
fail()  { echo -e "  ${RED}✗${NC} $*"; exit 1; }
info()  { echo -e "  → $*"; }

echo "========================================="
echo " DGX Dumb Deploy"
echo "========================================="

# ── 1. Check kubectl ──────────────────────────
step "Checking kubectl"
command -v kubectl >/dev/null 2>&1 || fail "kubectl not found"
ok "kubectl found"
if ! kubectl cluster-info >/dev/null 2>&1; then
  fail "cluster not reachable — check your KUBECONFIG"
fi
CONTEXT=$(kubectl config current-context 2>/dev/null || echo "?")
ok "cluster: $CONTEXT"

# ── 2. Check manifests file ───────────────────
step "Checking manifests"
[ -f "$MANIFESTS" ] || fail "manifests not found: $MANIFESTS"
LINES=$(wc -l < "$MANIFESTS" | tr -d ' ')
ok "manifests/dgx-all.yaml ($LINES lines)"

# ── 3. Namespace ──────────────────────────────
step "Creating namespace $NAMESPACE"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
ok "namespace ready"

# ── 4. Registry credentials (DockerHub) ──────
step "Configuring image pull secret (regcred → DockerHub)"

# Source local env.deploy if present (for non-interactive runs)
if [ -f "$SCRIPT_DIR/.env.deploy" ]; then
  # shellcheck disable=SC1091
  set -a; . "$SCRIPT_DIR/.env.deploy"; set +a
  ok "loaded .env.deploy"
fi

DH_USER="${DOCKERHUB_USER:-$REGISTRY_USERNAME_DEFAULT}"
DH_TOKEN="${DOCKERHUB_TOKEN:-}"

if [ -z "$DH_TOKEN" ]; then
  echo ""
  echo "  Enter DockerHub Personal Access Token (dckr_pat_...)"
  echo "  Get it from https://hub.docker.com/settings/security"
  echo "  (you can also export DOCKERHUB_TOKEN or fill $SCRIPT_DIR/.env.deploy)"
  read -r -s -p "  DOCKERHUB_TOKEN: " DH_TOKEN
  echo ""
fi
[ -z "$DH_TOKEN" ] && fail "empty DockerHub token"

kubectl -n "$NAMESPACE" delete secret regcred --ignore-not-found >/dev/null 2>&1
kubectl -n "$NAMESPACE" create secret docker-registry regcred \
  --docker-server="https://index.docker.io/v1/" \
  --docker-username="$DH_USER" \
  --docker-password="$DH_TOKEN" >/dev/null
ok "regcred created (server=DockerHub, user=$DH_USER)"

# ── 5. Apply manifests ────────────────────────
step "Applying manifests"
kubectl apply -f "$MANIFESTS"

# ── 6. Wait for postgres first ────────────────
step "Waiting for postgres"
if kubectl -n "$NAMESPACE" rollout status deploy/postgres --timeout=180s >/dev/null 2>&1; then
  ok "postgres ready"
else
  warn "postgres rollout not ready (continuing anyway)"
fi

# ── 6b. Bootstrap database schema ─────────────
SCHEMA_FILE="$SCRIPT_DIR/schema.sql"
if [ -f "$SCHEMA_FILE" ]; then
  step "Bootstrapping database schema"
  info "schema file: $SCHEMA_FILE ($(wc -c < "$SCHEMA_FILE" | tr -d ' ') bytes)"

  # Cleanup previous attempts
  kubectl -n "$NAMESPACE" delete pod apply-schema --ignore-not-found >/dev/null 2>&1
  kubectl -n "$NAMESPACE" delete configmap dm-schema --ignore-not-found >/dev/null 2>&1

  # Create ConfigMap with the schema
  if ! kubectl -n "$NAMESPACE" create configmap dm-schema --from-file=schema.sql="$SCHEMA_FILE" >/dev/null 2>&1; then
    fail "failed to create configmap dm-schema"
  fi
  CM_SIZE=$(kubectl -n "$NAMESPACE" get configmap dm-schema -o jsonpath='{.data.schema\.sql}' 2>/dev/null | wc -c | tr -d ' ')
  info "configmap dm-schema created (${CM_SIZE} bytes)"

  # Run psql via 'kubectl run' (Pod, not Job — supports --overrides for volumes)
  info "running psql to apply schema..."
  kubectl -n "$NAMESPACE" run apply-schema --restart=Never \
    --image=docker.io/<DOCKERHUB_NAMESPACE>/postgres:16-alpine \
    --overrides='{
      "spec": {
        "imagePullSecrets": [{"name": "regcred"}],
        "containers": [{
          "name": "apply-schema",
          "image": "docker.io/<DOCKERHUB_NAMESPACE>/postgres:16-alpine",
          "command": ["sh","-c","psql postgresql://postgres:postgres@postgres:5432/bootstrap -v ON_ERROR_STOP=1 -f /sql/schema.sql && echo SCHEMA_OK"],
          "volumeMounts": [{"name":"sql","mountPath":"/sql"}]
        }],
        "volumes": [{"name":"sql","configMap":{"name":"dm-schema"}}]
      }
    }' >/dev/null

  # Wait for the pod to finish (Succeeded or Failed)
  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24; do
    PHASE=$(kubectl -n "$NAMESPACE" get pod apply-schema -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
    case "$PHASE" in
      Succeeded) break ;;
      Failed)    break ;;
      "")        sleep 2 ;;
      *)         sleep 5 ;;
    esac
  done

  PHASE=$(kubectl -n "$NAMESPACE" get pod apply-schema -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
  LOGS=$(kubectl -n "$NAMESPACE" logs apply-schema 2>/dev/null || echo "")

  if [ "$PHASE" = "Succeeded" ] && echo "$LOGS" | grep -q "SCHEMA_OK"; then
    TABLES=$(echo "$LOGS" | grep -c "CREATE TABLE")
    ok "schema applied ($TABLES tables created)"
  elif echo "$LOGS" | grep -q "already exists"; then
    ok "schema already present"
  else
    warn "schema apply phase=$PHASE"
    echo "$LOGS" | tail -15
  fi

  # Cleanup
  kubectl -n "$NAMESPACE" delete pod apply-schema --ignore-not-found >/dev/null 2>&1
  kubectl -n "$NAMESPACE" delete configmap dm-schema --ignore-not-found >/dev/null 2>&1
else
  warn "schema.sql not found at $SCHEMA_FILE — skipping DB bootstrap"
fi

# ── 6c. Wait for all rollouts ─────────────────
step "Waiting for rollouts (max 3min each)"
DEPLOYS=$(kubectl -n "$NAMESPACE" get deploy -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "")
FAILED=0
for deploy in $DEPLOYS; do
  printf "  %-30s " "$deploy"
  if kubectl -n "$NAMESPACE" rollout status "deploy/$deploy" --timeout=180s >/dev/null 2>&1; then
    echo -e "${GREEN}ready${NC}"
  else
    echo -e "${RED}FAILED${NC}"
    FAILED=$((FAILED + 1))
  fi
done

# ── 6d. Restart queue-worker if it crashed waiting for schema ─
if kubectl -n "$NAMESPACE" get deploy queue-worker >/dev/null 2>&1; then
  CRASHED=$(kubectl -n "$NAMESPACE" get pods -l app=queue-worker -o jsonpath='{.items[*].status.containerStatuses[*].state.waiting.reason}' 2>/dev/null | grep -c CrashLoopBackOff || echo 0)
  if [ "$CRASHED" -gt 0 ]; then
    info "queue-worker had crashed (waiting for schema), restarting..."
    kubectl -n "$NAMESPACE" rollout restart deploy/queue-worker >/dev/null
    kubectl -n "$NAMESPACE" rollout status deploy/queue-worker --timeout=120s >/dev/null 2>&1 && \
      ok "queue-worker restarted" || warn "queue-worker still not ready"
  fi
fi

# ── 7. Final status ───────────────────────────
step "Final pod status"
kubectl -n "$NAMESPACE" get pods -o wide

echo ""
if [ "$FAILED" -eq 0 ]; then
  echo -e "${GREEN}========================================="
  echo -e " Deploy OK"
  echo -e "=========================================${NC}"
  echo ""
  echo "Next steps:"
  echo "  - Connectivity test : bash $SCRIPT_DIR/scripts/09-connectivity-test.sh"
  echo "  - Logs              : kubectl -n $NAMESPACE logs deploy/device-management"
  echo "  - Watch pods        : kubectl -n $NAMESPACE get pods -w"
  exit 0
else
  echo -e "${RED}========================================="
  echo -e " Deploy incomplete ($FAILED deployment(s) failed)"
  echo -e "=========================================${NC}"
  echo ""
  echo "Debug commands:"
  echo "  kubectl -n $NAMESPACE get events --sort-by=.lastTimestamp | tail -20"
  echo "  kubectl -n $NAMESPACE describe pod <pod-name>"
  echo "  kubectl -n $NAMESPACE logs <pod-name>"
  exit 1
fi
