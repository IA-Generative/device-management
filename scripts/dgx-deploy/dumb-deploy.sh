#!/usr/bin/env bash
# dumb-deploy.sh — deploiement DGX, une seule commande.
#
# Usage : ./dumb-deploy.sh
#
# Credentials dans ~/.dm-secrets/ (persistant entre les packages).
# Tokens applicatifs auto-generes au premier deploiement.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NAMESPACE="bootstrap"
MANIFESTS="$SCRIPT_DIR/manifests/dgx-all.yaml"
SECRET_NAME="device-management-secrets"
SECRETS_DIR="${DM_SECRETS_DIR:-$HOME/.dm-secrets}"
DEPLOY_CREDS="$SECRETS_DIR/.env.deploy"
SECRETS_FILE="$SECRETS_DIR/.env.secrets"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
step()  { echo ""; echo -e "${BLUE}▶ $*${NC}"; }
ok()    { echo -e "  ${GREEN}✓${NC} $*"; }
warn()  { echo -e "  ${YELLOW}⚠${NC} $*"; }
fail()  { echo -e "  ${RED}✗${NC} $*"; exit 1; }
info()  { echo -e "  → $*"; }

# Generate a random token (portable: python3 or openssl or /dev/urandom)
gen_token() {
  python3 -c "import secrets; print(secrets.token_urlsafe(32))" 2>/dev/null || \
  openssl rand -base64 32 2>/dev/null | tr -d '/+=' | head -c 43 || \
  head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 43
}

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
ok "cluster: $(kubectl config current-context 2>/dev/null || echo '?')"

# ── 2. Check manifests ────────────────────────
step "Checking manifests"
[ -f "$MANIFESTS" ] || fail "manifests not found: $MANIFESTS"
ok "manifests/dgx-all.yaml"

# ── 3. Setup ~/.dm-secrets/ ───────────────────
step "Checking credentials ($SECRETS_DIR)"

if [ ! -d "$SECRETS_DIR" ]; then
  mkdir -p "$SECRETS_DIR"
  chmod 700 "$SECRETS_DIR"
fi

# .env.deploy (DockerHub token)
if [ ! -f "$DEPLOY_CREDS" ]; then
  if [ -f "$SCRIPT_DIR/.env.deploy.example" ]; then
    cp "$SCRIPT_DIR/.env.deploy.example" "$DEPLOY_CREDS"
  else
    printf "DOCKERHUB_USER=<DOCKERHUB_NAMESPACE>\nDOCKERHUB_TOKEN=\n" > "$DEPLOY_CREDS"
  fi
  chmod 600 "$DEPLOY_CREDS"
  warn "fill in $DEPLOY_CREDS with your DockerHub token, then re-run"
  fail "credentials not configured"
fi

# .env.secrets (DB password + optional LLM token)
if [ ! -f "$SECRETS_FILE" ]; then
  if [ -f "$SCRIPT_DIR/.env.secrets.example" ]; then
    cp "$SCRIPT_DIR/.env.secrets.example" "$SECRETS_FILE"
  else
    printf "POSTGRES_PASSWORD=postgres\nLLM_API_TOKEN=\n" > "$SECRETS_FILE"
  fi
  chmod 600 "$SECRETS_FILE"
  ok "created $SECRETS_FILE with defaults"
fi

ok "credentials dir ready"
set -a; . "$DEPLOY_CREDS"; set +a

# ── 4. Namespace ──────────────────────────────
step "Creating namespace $NAMESPACE"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
ok "namespace ready"

# ── 5. Registry credentials ──────────────────
step "Configuring regcred"
DH_USER="${DOCKERHUB_USER:-<DOCKERHUB_NAMESPACE>}"
DH_TOKEN="${DOCKERHUB_TOKEN:-}"
[ -z "$DH_TOKEN" ] && fail "DOCKERHUB_TOKEN empty in $DEPLOY_CREDS"

kubectl -n "$NAMESPACE" delete secret regcred --ignore-not-found >/dev/null 2>&1
kubectl -n "$NAMESPACE" create secret docker-registry regcred \
  --docker-server="https://index.docker.io/v1/" \
  --docker-username="$DH_USER" \
  --docker-password="$DH_TOKEN" >/dev/null
ok "regcred (user=$DH_USER)"

# ── 6. Application secrets (auto-generated) ──
step "Managing secrets"

# Load user secrets
set -a; . "$SECRETS_FILE"; set +a

PG_PASS="${POSTGRES_PASSWORD:-postgres}"
LLM_TOKEN="${LLM_API_TOKEN:-}"

if kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" >/dev/null 2>&1; then
  ok "secret exists — PRESERVED"
  # Verify the admin token is usable (fix if it's a placeholder)
  CURRENT_TOKEN=$(kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" \
    -o jsonpath='{.data.DM_QUEUE_ADMIN_TOKEN}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
  if [ -z "$CURRENT_TOKEN" ] || echo "$CURRENT_TOKEN" | grep -qi "changeme"; then
    info "fixing placeholder tokens in existing secret..."
    MASTER_TOKEN=$(gen_token)
    SIGNING_KEY=$(gen_token)
    UPSTREAM_KEY=$(gen_token)
    kubectl -n "$NAMESPACE" patch secret "$SECRET_NAME" --type=merge -p "{\"stringData\":{
      \"DM_QUEUE_ADMIN_TOKEN\":\"$MASTER_TOKEN\",
      \"DM_RELAY_PROXY_SHARED_TOKEN\":\"$MASTER_TOKEN\",
      \"DM_RELAY_SECRET_PEPPER\":\"$MASTER_TOKEN\",
      \"ADMIN_SESSION_SECRET\":\"$MASTER_TOKEN\",
      \"DM_TELEMETRY_TOKEN_SIGNING_KEY\":\"$SIGNING_KEY\",
      \"DM_TELEMETRY_UPSTREAM_KEY\":\"$UPSTREAM_KEY\"
    }}" >/dev/null
    ok "placeholder tokens replaced with generated values"
    # Restart pods to pick up new tokens
    kubectl -n "$NAMESPACE" rollout restart deploy/device-management 2>/dev/null || true
    kubectl -n "$NAMESPACE" rollout restart deploy/device-management-admin 2>/dev/null || true
  fi
else
  info "creating secret with auto-generated tokens"

  # Generate one master token for all internal auth
  MASTER_TOKEN=$(gen_token)
  SIGNING_KEY=$(gen_token)
  UPSTREAM_KEY=$(gen_token)
  info "master token generated (used for relay, queue, session, pepper)"

  # Build the secret: .env.config (non-sensitive) + generated tokens + user secrets
  SECRET_ARGS=""

  # 1. Non-sensitive config from .env.config
  CONFIG_FILE="$SCRIPT_DIR/.env.config"
  if [ -f "$CONFIG_FILE" ]; then
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      case "$line" in \#*) continue ;; esac
      key="${line%%=*}"
      key=$(echo "$key" | tr -d '[:space:]')
      [ -z "$key" ] && continue
      value="${line#*=}"
      SECRET_ARGS="$SECRET_ARGS --from-literal=$key=$value"
    done < "$CONFIG_FILE"
  fi

  # 2. Database (derived from POSTGRES_PASSWORD)
  SECRET_ARGS="$SECRET_ARGS --from-literal=POSTGRES_PASSWORD=$PG_PASS"
  SECRET_ARGS="$SECRET_ARGS --from-literal=DATABASE_URL=postgresql://dev:dev@postgres:5432/bootstrap"
  SECRET_ARGS="$SECRET_ARGS --from-literal=DATABASE_ADMIN_URL=postgresql://postgres:${PG_PASS}@postgres:5432/bootstrap"

  # 3. Master token (reused for all internal auth — same value everywhere = no mismatch)
  SECRET_ARGS="$SECRET_ARGS --from-literal=DM_QUEUE_ADMIN_TOKEN=$MASTER_TOKEN"
  SECRET_ARGS="$SECRET_ARGS --from-literal=DM_RELAY_PROXY_SHARED_TOKEN=$MASTER_TOKEN"
  SECRET_ARGS="$SECRET_ARGS --from-literal=DM_RELAY_SECRET_PEPPER=$MASTER_TOKEN"
  SECRET_ARGS="$SECRET_ARGS --from-literal=ADMIN_SESSION_SECRET=$MASTER_TOKEN"

  # 4. Telemetry signing keys (separate from master token for crypto hygiene)
  SECRET_ARGS="$SECRET_ARGS --from-literal=DM_TELEMETRY_TOKEN_SIGNING_KEY=$SIGNING_KEY"
  SECRET_ARGS="$SECRET_ARGS --from-literal=DM_TELEMETRY_UPSTREAM_KEY=$UPSTREAM_KEY"

  # 5. LLM + AWS from user secrets
  SECRET_ARGS="$SECRET_ARGS --from-literal=LLM_API_TOKEN=$LLM_TOKEN"
  SECRET_ARGS="$SECRET_ARGS --from-literal=AWS_REGION=${AWS_REGION:-}"
  SECRET_ARGS="$SECRET_ARGS --from-literal=AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID:-}"
  SECRET_ARGS="$SECRET_ARGS --from-literal=AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY:-}"
  SECRET_ARGS="$SECRET_ARGS --from-literal=AWS_SESSION_TOKEN=${AWS_SESSION_TOKEN:-}"

  eval kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" $SECRET_ARGS >/dev/null
  ok "secret created (tokens auto-generated)"
fi

# ── 7. Apply manifests ────────────────────────
step "Applying manifests"
kubectl apply -f "$MANIFESTS"

# ── 8. Scale to 1 replica (DGX single node) ──
step "Scaling to 1 replica"
for deploy in device-management queue-worker; do
  CURRENT=$(kubectl -n "$NAMESPACE" get deploy "$deploy" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo "0")
  if [ "$CURRENT" != "1" ]; then
    kubectl -n "$NAMESPACE" scale deploy/"$deploy" --replicas=1 >/dev/null
    info "$deploy: $CURRENT → 1"
  else
    ok "$deploy: already 1"
  fi
done

# ── 9. Wait for postgres ─────────────────────
step "Waiting for postgres"
kubectl -n "$NAMESPACE" rollout status deploy/postgres --timeout=180s >/dev/null 2>&1 && \
  ok "postgres ready" || warn "postgres not ready"

# ── 10. Bootstrap schema ─────────────────────
SCHEMA_FILE="$SCRIPT_DIR/schema.sql"
if [ -f "$SCHEMA_FILE" ]; then
  step "Bootstrapping database schema"

  kubectl -n "$NAMESPACE" delete pod apply-schema --ignore-not-found >/dev/null 2>&1
  kubectl -n "$NAMESPACE" delete configmap dm-schema --ignore-not-found >/dev/null 2>&1
  kubectl -n "$NAMESPACE" create configmap dm-schema --from-file=schema.sql="$SCHEMA_FILE" >/dev/null

  kubectl -n "$NAMESPACE" run apply-schema --restart=Never \
    --image=docker.io/<DOCKERHUB_NAMESPACE>/postgres:16-alpine \
    --overrides='{
      "spec": {
        "imagePullSecrets": [{"name": "regcred"}],
        "containers": [{
          "name": "apply-schema",
          "image": "docker.io/<DOCKERHUB_NAMESPACE>/postgres:16-alpine",
          "command": ["sh","-c","until pg_isready -h postgres -p 5432 -t 60; do sleep 2; done && psql $DATABASE_ADMIN_URL -v ON_ERROR_STOP=1 -f /sql/schema.sql && echo SCHEMA_OK"],
          "env": [{"name":"DATABASE_ADMIN_URL","valueFrom":{"secretKeyRef":{"name":"device-management-secrets","key":"DATABASE_ADMIN_URL"}}}],
          "volumeMounts": [{"name":"sql","mountPath":"/sql"}]
        }],
        "volumes": [{"name":"sql","configMap":{"name":"dm-schema"}}]
      }
    }' >/dev/null

  for i in $(seq 1 30); do
    PHASE=$(kubectl -n "$NAMESPACE" get pod apply-schema -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
    case "$PHASE" in Succeeded|Failed) break ;; esac
    sleep 5
  done

  LOGS=$(kubectl -n "$NAMESPACE" logs apply-schema 2>/dev/null || echo "")
  if echo "$LOGS" | grep -q "SCHEMA_OK"; then
    ok "schema applied"
  elif echo "$LOGS" | grep -q "already exists"; then
    ok "schema already present"
  else
    warn "schema: $(echo "$LOGS" | tail -3)"
  fi

  kubectl -n "$NAMESPACE" delete pod apply-schema --ignore-not-found >/dev/null 2>&1
  kubectl -n "$NAMESPACE" delete configmap dm-schema --ignore-not-found >/dev/null 2>&1
fi

# ── 11. Wait for rollouts ─────────────────────
step "Waiting for rollouts"
FAILED=0
for deploy in $(kubectl -n "$NAMESPACE" get deploy -o jsonpath='{.items[*].metadata.name}' 2>/dev/null); do
  printf "  %-30s " "$deploy"
  if kubectl -n "$NAMESPACE" rollout status "deploy/$deploy" --timeout=180s >/dev/null 2>&1; then
    echo -e "${GREEN}ready${NC}"
  else
    echo -e "${RED}FAILED${NC}"
    FAILED=$((FAILED + 1))
  fi
done

# Restart queue-worker if crashed
CRASHED=$(kubectl -n "$NAMESPACE" get pods -l app=queue-worker -o jsonpath='{.items[*].status.containerStatuses[*].state.waiting.reason}' 2>/dev/null | grep -c CrashLoopBackOff || true)
if [ "${CRASHED:-0}" -gt 0 ]; then
  info "restarting queue-worker..."
  kubectl -n "$NAMESPACE" rollout restart deploy/queue-worker >/dev/null
  kubectl -n "$NAMESPACE" rollout status deploy/queue-worker --timeout=120s >/dev/null 2>&1 || true
fi

# ── 12. Final status ──────────────────────────
step "Pod status"
kubectl -n "$NAMESPACE" get pods -o wide

echo ""
if [ "$FAILED" -eq 0 ]; then
  echo -e "${GREEN}========================================="
  echo -e " Deploy OK"
  echo -e "=========================================${NC}"
  exit 0
else
  echo -e "${RED}========================================="
  echo -e " $FAILED deployment(s) failed"
  echo -e "=========================================${NC}"
  echo "  kubectl -n $NAMESPACE get events --sort-by=.lastTimestamp | tail -20"
  exit 1
fi
