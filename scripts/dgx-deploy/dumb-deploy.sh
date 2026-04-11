#!/usr/bin/env bash
# dumb-deploy.sh — deploiement DGX, une seule commande.
#
# Prerequis : kubectl configure sur le cluster cible.
# Usage :     ./dumb-deploy.sh
#
# Credentials et secrets sont stockes dans ~/.dm-secrets/ (persiste entre deploys).
# Au premier lancement, le script copie les templates et demande de les remplir.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NAMESPACE="bootstrap"
REGISTRY_USERNAME_DEFAULT="etiquet"
MANIFESTS="$SCRIPT_DIR/manifests/dgx-all.yaml"
SECRET_NAME="device-management-secrets"

# Repertoire persistant pour credentials + secrets (hors du package)
SECRETS_DIR="${DM_SECRETS_DIR:-$HOME/.dm-secrets}"
DEPLOY_CREDS="$SECRETS_DIR/.env.deploy"
SECRETS_FILE="$SECRETS_DIR/.env.secrets"

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

# ── 4. Persistent secrets directory ───────────
step "Checking credentials ($SECRETS_DIR)"

if [ ! -d "$SECRETS_DIR" ]; then
  mkdir -p "$SECRETS_DIR"
  chmod 700 "$SECRETS_DIR"
  ok "created $SECRETS_DIR"
fi

# Copy templates if first time
if [ ! -f "$DEPLOY_CREDS" ]; then
  if [ -f "$SCRIPT_DIR/.env.deploy.example" ]; then
    cp "$SCRIPT_DIR/.env.deploy.example" "$DEPLOY_CREDS"
  else
    printf "DOCKERHUB_USER=etiquet\nDOCKERHUB_TOKEN=\n" > "$DEPLOY_CREDS"
  fi
  chmod 600 "$DEPLOY_CREDS"
  warn "NEW: fill in $DEPLOY_CREDS with your DockerHub token"
  echo ""
  echo "  nano $DEPLOY_CREDS"
  echo ""
  fail "credentials not configured yet — fill $DEPLOY_CREDS and re-run"
fi

if [ ! -f "$SECRETS_FILE" ]; then
  if [ -f "$SCRIPT_DIR/.env.secrets.example" ]; then
    cp "$SCRIPT_DIR/.env.secrets.example" "$SECRETS_FILE"
  fi
  chmod 600 "$SECRETS_FILE"
  warn "NEW: fill in $SECRETS_FILE with your production secrets"
  echo ""
  echo "  nano $SECRETS_FILE"
  echo ""
  fail "secrets not configured yet — fill $SECRETS_FILE and re-run"
fi

ok "credentials dir: $SECRETS_DIR"

# Load credentials
set -a; . "$DEPLOY_CREDS"; set +a

# ── 5. Registry credentials (DockerHub) ──────
step "Configuring image pull secret (regcred)"

DH_USER="${DOCKERHUB_USER:-$REGISTRY_USERNAME_DEFAULT}"
DH_TOKEN="${DOCKERHUB_TOKEN:-}"
[ -z "$DH_TOKEN" ] && fail "DOCKERHUB_TOKEN empty in $DEPLOY_CREDS"

kubectl -n "$NAMESPACE" delete secret regcred --ignore-not-found >/dev/null 2>&1
kubectl -n "$NAMESPACE" create secret docker-registry regcred \
  --docker-server="https://index.docker.io/v1/" \
  --docker-username="$DH_USER" \
  --docker-password="$DH_TOKEN" >/dev/null
ok "regcred created (user=$DH_USER)"

# ── 6. Application secrets ───────────────────
step "Managing application secrets"

if kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" >/dev/null 2>&1; then
  ok "secret $SECRET_NAME already exists — PRESERVED"
  info "(to recreate: kubectl -n $NAMESPACE delete secret $SECRET_NAME && re-run)"
else
  info "creating secret from $SECRETS_FILE"

  # Build --from-literal args from .env.secrets (sensitive keys)
  SECRET_ARGS=""
  while IFS= read -r line; do
    # Skip comments and empty lines
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    key="${line%%=*}"
    value="${line#*=}"
    key=$(echo "$key" | tr -d '[:space:]')
    [ -z "$key" ] && continue
    SECRET_ARGS="$SECRET_ARGS --from-literal=$key=$value"
  done < "$SECRETS_FILE"

  # Add non-sensitive config keys (URLs, flags) that the app also needs
  CONFIG_FILE="$SCRIPT_DIR/.env.config"
  if [ -f "$CONFIG_FILE" ]; then
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      case "$line" in \#*) continue ;; esac
      key="${line%%=*}"
      key=$(echo "$key" | tr -d '[:space:]')
      [ -z "$key" ] && continue
      # Secrets take priority over config
      if ! grep -q "^${key}=" "$SECRETS_FILE" 2>/dev/null; then
        value="${line#*=}"
        SECRET_ARGS="$SECRET_ARGS --from-literal=$key=$value"
      fi
    done < "$CONFIG_FILE"
  fi

  eval kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" $SECRET_ARGS >/dev/null
  ok "secret created"
fi

# ── 6. Apply manifests (secret excluded) ─────
step "Applying manifests (secret managed separately)"
kubectl apply -f "$MANIFESTS"

# ── 7. Wait for postgres first ────────────────
step "Waiting for postgres"
if kubectl -n "$NAMESPACE" rollout status deploy/postgres --timeout=180s >/dev/null 2>&1; then
  ok "postgres ready"
else
  warn "postgres rollout not ready (continuing anyway)"
fi

# ── 7b. Bootstrap database schema ─────────────
SCHEMA_FILE="$SCRIPT_DIR/schema.sql"
if [ -f "$SCHEMA_FILE" ]; then
  step "Bootstrapping database schema"
  info "schema file: $SCHEMA_FILE ($(wc -c < "$SCHEMA_FILE" | tr -d ' ') bytes)"

  kubectl -n "$NAMESPACE" delete pod apply-schema --ignore-not-found >/dev/null 2>&1
  kubectl -n "$NAMESPACE" delete configmap dm-schema --ignore-not-found >/dev/null 2>&1

  if ! kubectl -n "$NAMESPACE" create configmap dm-schema --from-file=schema.sql="$SCHEMA_FILE" >/dev/null 2>&1; then
    fail "failed to create configmap dm-schema"
  fi

  info "running psql to apply schema..."
  kubectl -n "$NAMESPACE" run apply-schema --restart=Never \
    --image=docker.io/etiquet/postgres:16-alpine \
    --overrides='{
      "spec": {
        "imagePullSecrets": [{"name": "regcred"}],
        "containers": [{
          "name": "apply-schema",
          "image": "docker.io/etiquet/postgres:16-alpine",
          "command": ["sh","-c","psql $DATABASE_ADMIN_URL -v ON_ERROR_STOP=1 -f /sql/schema.sql && echo SCHEMA_OK"],
          "env": [{"name":"DATABASE_ADMIN_URL","valueFrom":{"secretKeyRef":{"name":"device-management-secrets","key":"DATABASE_ADMIN_URL"}}}],
          "volumeMounts": [{"name":"sql","mountPath":"/sql"}]
        }],
        "volumes": [{"name":"sql","configMap":{"name":"dm-schema"}}]
      }
    }' >/dev/null

  for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24; do
    PHASE=$(kubectl -n "$NAMESPACE" get pod apply-schema -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
    case "$PHASE" in Succeeded|Failed) break ;; esac
    sleep 5
  done

  LOGS=$(kubectl -n "$NAMESPACE" logs apply-schema 2>/dev/null || echo "")
  if echo "$LOGS" | grep -q "SCHEMA_OK"; then
    TABLES=$(echo "$LOGS" | grep -c "CREATE TABLE" || echo 0)
    ok "schema applied ($TABLES tables)"
  elif echo "$LOGS" | grep -q "already exists"; then
    ok "schema already present"
  else
    warn "schema status: $(echo "$LOGS" | tail -3)"
  fi

  kubectl -n "$NAMESPACE" delete pod apply-schema --ignore-not-found >/dev/null 2>&1
  kubectl -n "$NAMESPACE" delete configmap dm-schema --ignore-not-found >/dev/null 2>&1
else
  warn "schema.sql not found — skipping DB bootstrap"
fi

# ── 8. Wait for all rollouts ─────────────────
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

# Restart queue-worker if it crashed waiting for schema
if kubectl -n "$NAMESPACE" get deploy queue-worker >/dev/null 2>&1; then
  CRASHED=$(kubectl -n "$NAMESPACE" get pods -l app=queue-worker -o jsonpath='{.items[*].status.containerStatuses[*].state.waiting.reason}' 2>/dev/null | grep -c CrashLoopBackOff || true)
  if [ "${CRASHED:-0}" -gt 0 ]; then
    info "queue-worker crashed (waiting for schema), restarting..."
    kubectl -n "$NAMESPACE" rollout restart deploy/queue-worker >/dev/null
    kubectl -n "$NAMESPACE" rollout status deploy/queue-worker --timeout=120s >/dev/null 2>&1 && \
      ok "queue-worker restarted" || warn "queue-worker still not ready"
  fi
fi

# ── 9. Final status ───────────────────────────
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
  echo "Debug:"
  echo "  kubectl -n $NAMESPACE get events --sort-by=.lastTimestamp | tail -20"
  echo "  kubectl -n $NAMESPACE describe pod <pod-name>"
  echo "  kubectl -n $NAMESPACE logs <pod-name>"
  exit 1
fi
