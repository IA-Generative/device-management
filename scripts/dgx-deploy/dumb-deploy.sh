#!/usr/bin/env bash
# dumb-deploy.sh — deploiement DGX, une seule commande.
#
# Prerequis : kubectl configure sur le cluster cible.
# Usage :
#   ./dumb-deploy.sh                       # deploy (secrets preserved if exist)
#   ./dumb-deploy.sh --reset-secrets       # force re-creation of secrets from .env.secrets
#   ./dumb-deploy.sh --import-secrets DIR  # import secrets from a previous deploy directory
#
# Ce que fait le script :
#   1. Verifie kubectl + cluster joignable
#   2. Cree le namespace bootstrap
#   3. Cree/met a jour le secret regcred (credentials DockerHub)
#   4. Gere les secrets applicatifs (cree une seule fois, jamais ecrases)
#   5. Applique les manifests (sans le Secret, qui est gere separement)
#   6. Bootstrap le schema postgres si necessaire
#   7. Attend que les Deployments soient prets
#   8. Affiche l'etat final
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NAMESPACE="bootstrap"
REGISTRY_SERVER="docker.io"
REGISTRY_USERNAME_DEFAULT="etiquet"
MANIFESTS="$SCRIPT_DIR/manifests/dgx-all.yaml"
SECRETS_FILE="$SCRIPT_DIR/.env.secrets"
SECRETS_EXAMPLE="$SCRIPT_DIR/.env.secrets.example"
SECRET_NAME="device-management-secrets"

# Parse args
RESET_SECRETS=false
IMPORT_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --reset-secrets) RESET_SECRETS=true; shift ;;
    --import-secrets) IMPORT_DIR="$2"; shift 2 ;;
    *) shift ;;
  esac
done

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
step "Configuring image pull secret (regcred)"

if [ -f "$SCRIPT_DIR/.env.deploy" ]; then
  set -a; . "$SCRIPT_DIR/.env.deploy"; set +a
  ok "loaded .env.deploy"
fi

DH_USER="${DOCKERHUB_USER:-$REGISTRY_USERNAME_DEFAULT}"
DH_TOKEN="${DOCKERHUB_TOKEN:-}"

if [ -z "$DH_TOKEN" ]; then
  echo ""
  echo "  Enter DockerHub Personal Access Token (dckr_pat_...)"
  read -r -s -p "  DOCKERHUB_TOKEN: " DH_TOKEN
  echo ""
fi
[ -z "$DH_TOKEN" ] && fail "empty DockerHub token"

kubectl -n "$NAMESPACE" delete secret regcred --ignore-not-found >/dev/null 2>&1
kubectl -n "$NAMESPACE" create secret docker-registry regcred \
  --docker-server="https://index.docker.io/v1/" \
  --docker-username="$DH_USER" \
  --docker-password="$DH_TOKEN" >/dev/null
ok "regcred created (user=$DH_USER)"

# ── 5. Application secrets ───────────────────
step "Managing application secrets ($SECRET_NAME)"

# Import from previous deploy directory if requested
if [ -n "$IMPORT_DIR" ]; then
  if [ -f "$IMPORT_DIR/.env.secrets" ]; then
    cp "$IMPORT_DIR/.env.secrets" "$SECRETS_FILE"
    ok "imported .env.secrets from $IMPORT_DIR"
  else
    warn "no .env.secrets found in $IMPORT_DIR"
  fi
fi

SECRET_EXISTS=false
if kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" >/dev/null 2>&1; then
  SECRET_EXISTS=true
fi

if [ "$SECRET_EXISTS" = "true" ] && [ "$RESET_SECRETS" = "false" ]; then
  # Secret exists and no reset requested — keep it
  KEYS=$(kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" -o jsonpath='{range .data[*]}{end}' 2>/dev/null | wc -c | tr -d ' ')
  ok "secret already exists ($SECRET_NAME) — PRESERVED (use --reset-secrets to overwrite)"

  # Back up current secrets to .env.secrets.backup for safety
  info "backing up current secrets to .env.secrets.backup"
  kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" -o jsonpath='{range .data[*]}{end}' >/dev/null 2>&1
  kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" -o json 2>/dev/null | \
    python3 -c "
import sys, json, base64
s = json.load(sys.stdin)
data = s.get('data', {})
with open('$SCRIPT_DIR/.env.secrets.backup', 'w') as f:
    f.write('# Backup of $SECRET_NAME from cluster ($(date -u +%Y-%m-%dT%H:%M:%SZ))\n')
    for k in sorted(data.keys()):
        v = base64.b64decode(data[k]).decode('utf-8', errors='replace')
        f.write(f'{k}={v}\n')
" 2>/dev/null && ok "backup saved to .env.secrets.backup" || warn "backup failed (python3 not available?)"

else
  # Secret doesn't exist or reset requested — create from .env.secrets
  if [ ! -f "$SECRETS_FILE" ]; then
    if [ -f "$SECRETS_EXAMPLE" ]; then
      warn ".env.secrets not found"
      echo ""
      echo "  Create it from the example:"
      echo "    cp .env.secrets.example .env.secrets"
      echo "    \$EDITOR .env.secrets"
      echo ""
      echo "  Or import from a previous deploy:"
      echo "    ./dumb-deploy.sh --import-secrets /path/to/old/dgx-deploy-vX.X/"
      echo ""
      fail ".env.secrets required for first deployment"
    else
      fail ".env.secrets not found and no example template available"
    fi
  fi

  info "loading secrets from .env.secrets"

  # Read .env.secrets and build kubectl create secret command
  # The secret combines: .env.secrets (sensitive) + non-sensitive config from kustomize
  # We render the full secret from kustomize to get the non-sensitive keys,
  # then overlay the sensitive keys from .env.secrets

  # Start with all keys from the rendered kustomize secret (config values)
  # These are extracted from the kustomization source
  KUSTOMIZE_SECRET=$(kubectl kustomize "$SCRIPT_DIR/manifests/dgx-overlay" 2>/dev/null || echo "")

  # Build the secret from the .env.secrets file
  SECRET_ARGS=""
  while IFS='=' read -r key value; do
    # Skip comments and empty lines
    [ -z "$key" ] && continue
    [[ "$key" =~ ^[[:space:]]*# ]] && continue
    # Remove leading/trailing whitespace
    key=$(echo "$key" | tr -d '[:space:]')
    # Value is everything after first =
    SECRET_ARGS="$SECRET_ARGS --from-literal=$key=$value"
  done < "$SECRETS_FILE"

  # Also include the non-sensitive config keys that the app needs
  # These come from the kustomize overlay secret-patch
  NON_SENSITIVE_KEYS="
    DM_APP_ENV=prod
    DM_CONFIG_ENABLED=true
    DM_CONFIG_PROFILE=prod
    DM_ENROLL_URL=/bootstrap/enroll
    DM_ALLOW_ORIGINS=*
    DM_MAX_BODY_SIZE_MB=10
    DM_PORT=3001
    DM_STORE_ENROLL_LOCALLY=true
    DM_ENROLL_DIR=/data/enroll
    DM_STORE_ENROLL_S3=false
    DM_AUTH_VERIFY_ACCESS_TOKEN=true
    DM_AUTH_ALLOWED_ALGORITHMS_CSV=RS256
    DM_AUTH_LEEWAY_SECONDS=30
    DM_AUTH_JWKS_CACHE_TTL_SECONDS=600
    DM_RELAY_ENABLED=true
    DM_RELAY_KEY_TTL_SECONDS=2592000
    DM_RELAY_REQUIRE_KEY_FOR_SECRETS=true
    DM_RELAY_ASSISTANT_URL=http://relay-assistant
    DM_TELEMETRY_ENABLED=true
    DM_TELEMETRY_PUBLIC_ENDPOINT=/telemetry/v1/traces
    DM_TELEMETRY_AUTHORIZATION_TYPE=Bearer
    DM_TELEMETRY_UPSTREAM_AUTH_TYPE=Bearer
    DM_TELEMETRY_UPSTREAM_ENDPOINT=http://otel-collector.telemetry.svc.cluster.local:4318/v1/traces
    DM_TELEMETRY_REQUIRE_TOKEN=true
    DM_TELEMETRY_TOKEN_TTL_SECONDS=300
    DM_TELEMETRY_MAX_BODY_SIZE_MB=2
    DM_BINARIES_MODE=local
    DM_PRESIGN_TTL_SECONDS=300
    DM_S3_BUCKET=
    DM_S3_PREFIX_ENROLL=enroll/
    DM_S3_PREFIX_BINARIES=binaries/
    DM_S3_ENDPOINT_URL=
    KEYCLOAK_REDIRECT_URI=http://localhost:28443/callback
    KEYCLOAK_ALLOWED_REDIRECT_URI=http://localhost:28443/callback
    DM_AUTH_AUDIENCE=
    DM_TELEMETRY_GRAFANA_URL=
    TELEMETRY_SALT=
    TELEMETRY_KEY=
    DEFAULT_MODEL_NAME=
    RELAY_MCR_API_UPSTREAM=
    AWS_DEFAULT_ORGANIZATION_ID=
    AWS_DEFAULT_PROJECT_ID=
    PUBLIC_BASE_URL=https://<DGX_HOSTNAME>/bootstrap
    KEYCLOAK_ISSUER_URL=https://<SSO_HOSTNAME>
    KEYCLOAK_REALM=mirai
    KEYCLOAK_CLIENT_ID=bootstrap-iassistant
    ADMIN_REQUIRED_GROUP=/g/Iassistant-Device-management
    ADMIN_OIDC_ISSUER_URL=https://<SSO_HOSTNAME>/realms/mirai
    ADMIN_OIDC_PUBLIC_ISSUER_URL=https://<SSO_HOSTNAME>/realms/mirai
    ADMIN_OIDC_REDIRECT_URI=https://<DGX_HOSTNAME>/admin/callback
    DM_AUTH_JWKS_URL=https://<SSO_HOSTNAME>/realms/mirai/protocol/openid-connect/certs
    RELAY_KEYCLOAK_UPSTREAM=https://<SSO_HOSTNAME>/realms/mirai
    RELAY_COMPTE_RENDU_UPSTREAM=https://<COMPTERENDU_HOSTNAME>
    DM_RELAY_ALLOWED_TARGETS_CSV=keycloak,config,llm,mcr-api,telemetry,compte-rendu
    LLM_BASE_URL=https://<LLM_API_HOSTNAME>/v1
    RELAY_LLM_UPSTREAM=https://<LLM_API_HOSTNAME>/v1
  "

  for entry in $NON_SENSITIVE_KEYS; do
    key="${entry%%=*}"
    value="${entry#*=}"
    # Only add if not already in .env.secrets (secrets take priority)
    if ! grep -q "^${key}=" "$SECRETS_FILE" 2>/dev/null; then
      SECRET_ARGS="$SECRET_ARGS --from-literal=$key=$value"
    fi
  done

  kubectl -n "$NAMESPACE" delete secret "$SECRET_NAME" --ignore-not-found >/dev/null 2>&1
  eval kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" $SECRET_ARGS >/dev/null
  ok "secret created from .env.secrets"
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
          "command": ["sh","-c","psql postgresql://postgres:postgres@postgres:5432/bootstrap -v ON_ERROR_STOP=1 -f /sql/schema.sql && echo SCHEMA_OK"],
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
