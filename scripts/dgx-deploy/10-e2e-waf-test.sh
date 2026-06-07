#!/usr/bin/env bash
# scripts/dgx-deploy/10-e2e-waf-test.sh
# ---------------------------------------------------------------------------
# End-to-end test suite that validates all admin/API flows through a
# simulated WAF (nginx that blocks native browser form POSTs) + Envoy Gateway.
#
# What it does:
#   1. Installs Envoy Gateway (helm)
#   2. Deploys a WAF simulator (nginx with Sec-Fetch-Mode blocking)
#   3. Deploys device-management + admin + postgres with schema
#   4. Runs the full test suite from a curl pod
#   5. Cleans up everything
#
# Prerequisites: kubectl, helm, docker login to DockerHub (<DOCKERHUB_NAMESPACE>)
# Usage: bash scripts/dgx-deploy/10-e2e-waf-test.sh
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
NS="dgx-e2e-test"
IMAGE="${DM_IMAGE:-docker.io/<DOCKERHUB_NAMESPACE>/device-management:0.5.16-waf-fix}"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
step()  { echo ""; echo -e "${BLUE}▶ $*${NC}"; }
ok()    { echo -e "  ${GREEN}✓${NC} $*"; }
warn()  { echo -e "  ${YELLOW}⚠${NC} $*"; }
fail()  { echo -e "  ${RED}✗${NC} $*"; }
info()  { echo -e "  → $*"; }

cleanup() {
  step "Cleaning up"
  kubectl delete namespace "$NS" --wait=false 2>/dev/null || true
  kubectl delete gatewayclass eg --wait=false 2>/dev/null || true
  helm uninstall eg -n envoy-gateway-system 2>/dev/null || true
  kubectl delete namespace envoy-gateway-system --wait=false 2>/dev/null || true
  ok "cleanup done"
}

trap cleanup EXIT

echo "═══════════════════════════════════════════════"
echo " E2E WAF + Envoy Gateway Test Suite"
echo " Image: $IMAGE"
echo "═══════════════════════════════════════════════"

# ── 1. Install Envoy Gateway ─────────────────────
step "Installing Envoy Gateway"
# Retry helm install (CRDs from previous run may still be terminating)
for attempt in 1 2 3; do
  if helm install eg oci://docker.io/envoyproxy/gateway-helm --version v1.6.2 \
    -n envoy-gateway-system --create-namespace >/dev/null 2>&1; then
    break
  fi
  info "helm install attempt $attempt failed, retrying in 10s..."
  helm uninstall eg -n envoy-gateway-system 2>/dev/null || true
  sleep 10
done
kubectl -n envoy-gateway-system rollout status deploy/envoy-gateway --timeout=120s >/dev/null 2>&1
cat <<'EOF' | kubectl apply -f - >/dev/null
apiVersion: gateway.networking.k8s.io/v1
kind: GatewayClass
metadata: { name: eg }
spec: { controllerName: gateway.envoyproxy.io/gatewayclass-controller }
---
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata: { name: api-gateway, namespace: envoy-gateway-system }
spec:
  gatewayClassName: eg
  listeners: [{ name: http, protocol: HTTP, port: 80, allowedRoutes: { namespaces: { from: All } } }]
EOF
ok "Envoy Gateway ready"

# ── 2. Create namespace + regcred ─────────────────
step "Creating namespace $NS"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# Load DockerHub credentials
if [ -f "$ROOT_DIR/.env.registry" ]; then
  set -a; source "$ROOT_DIR/.env.registry"; set +a
fi
DH_USER="${DOCKERHUB_USER:-<DOCKERHUB_NAMESPACE>}"
DH_TOKEN="${DOCKERHUB_TOKEN:-}"
if [ -n "$DH_TOKEN" ]; then
  kubectl -n "$NS" create secret docker-registry regcred \
    --docker-server=https://index.docker.io/v1/ \
    --docker-username="$DH_USER" \
    --docker-password="$DH_TOKEN" >/dev/null 2>&1
  ok "regcred created"
else
  warn "no DOCKERHUB_TOKEN — images may fail to pull"
fi

# ── 3. Deploy WAF + App + Postgres ────────────────
step "Deploying WAF + app + postgres"
cat <<EOF | kubectl apply -f - >/dev/null
---
apiVersion: v1
kind: ConfigMap
metadata: { name: waf-nginx-config, namespace: $NS }
data:
  default.conf: |
    server {
      listen 8080;
      resolver 10.32.0.10 valid=10s ipv6=off;
      set \$block_post 0;
      if (\$request_method = POST) { set \$block_post 1; }
      if (\$http_sec_fetch_mode = "cors") { set \$block_post 0; }
      if (\$http_sec_fetch_mode = "same-origin") { set \$block_post 0; }
      if (\$block_post = 1) {
        return 403 '{"error":"WAF: native form POST blocked"}';
      }
      location / {
        proxy_pass http://envoy-envoy-gateway-system-api-gateway-c6bcb0e4.envoy-gateway-system.svc.cluster.local:80;
        proxy_set_header Host \$host;
        proxy_http_version 1.1;
        proxy_request_buffering off;
        client_max_body_size 50m;
      }
    }
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: waf-proxy, namespace: $NS }
spec:
  replicas: 1
  selector: { matchLabels: { app: waf-proxy } }
  template:
    metadata: { labels: { app: waf-proxy } }
    spec:
      imagePullSecrets: [{ name: regcred }]
      containers:
        - name: nginx
          image: docker.io/<DOCKERHUB_NAMESPACE>/nginx:1.29-alpine
          ports: [{ containerPort: 8080 }]
          volumeMounts: [{ name: conf, mountPath: /etc/nginx/conf.d/default.conf, subPath: default.conf }]
      volumes: [{ name: conf, configMap: { name: waf-nginx-config } }]
---
apiVersion: v1
kind: Service
metadata: { name: waf-proxy, namespace: $NS }
spec:
  selector: { app: waf-proxy }
  ports: [{ port: 8080 }]
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: postgres, namespace: $NS }
spec:
  replicas: 1
  selector: { matchLabels: { app: postgres } }
  template:
    metadata: { labels: { app: postgres } }
    spec:
      imagePullSecrets: [{ name: regcred }]
      containers:
        - name: postgres
          image: docker.io/<DOCKERHUB_NAMESPACE>/postgres:16-alpine
          env: [{ name: POSTGRES_PASSWORD, value: postgres }, { name: POSTGRES_DB, value: bootstrap }]
---
apiVersion: v1
kind: Service
metadata: { name: postgres, namespace: $NS }
spec:
  selector: { app: postgres }
  ports: [{ port: 5432 }]
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: device-management, namespace: $NS }
spec:
  replicas: 1
  selector: { matchLabels: { app: device-management } }
  template:
    metadata: { labels: { app: device-management } }
    spec:
      imagePullSecrets: [{ name: regcred }]
      initContainers:
        - { name: w, image: docker.io/<DOCKERHUB_NAMESPACE>/postgres:16-alpine, command: ["sh","-c","until pg_isready -h postgres; do sleep 1; done"] }
      containers:
        - name: device-management
          image: $IMAGE
          env:
            - { name: DM_RUNTIME_MODE, value: api }
            - { name: DATABASE_URL, value: "postgresql://dev:dev@postgres:5432/bootstrap" }
            - { name: DATABASE_ADMIN_URL, value: "postgresql://postgres:postgres@postgres:5432/postgres" }
            - { name: DM_ALLOW_ORIGINS, value: "*" }
            - { name: DM_BINARIES_MODE, value: local }
            - { name: DM_STORE_ENROLL_LOCALLY, value: "true" }
            - { name: DM_TELEMETRY_ENABLED, value: "false" }
            - { name: DM_RELAY_ENABLED, value: "false" }
            - { name: DM_CONFIG_ENABLED, value: "true" }
---
apiVersion: v1
kind: Service
metadata: { name: device-management, namespace: $NS }
spec:
  selector: { app: device-management }
  ports: [{ port: 80, targetPort: 3001 }]
---
apiVersion: apps/v1
kind: Deployment
metadata: { name: device-management-admin, namespace: $NS }
spec:
  replicas: 1
  selector: { matchLabels: { app: device-management-admin } }
  template:
    metadata: { labels: { app: device-management-admin } }
    spec:
      imagePullSecrets: [{ name: regcred }]
      initContainers:
        - { name: w, image: docker.io/<DOCKERHUB_NAMESPACE>/postgres:16-alpine, command: ["sh","-c","until pg_isready -h postgres; do sleep 1; done"] }
      containers:
        - name: device-management-admin
          image: $IMAGE
          env:
            - { name: DM_RUNTIME_MODE, value: admin }
            - { name: ADMIN_SESSION_SECRET, value: changeme-dev-only }
            - { name: DATABASE_URL, value: "postgresql://dev:dev@postgres:5432/bootstrap" }
            - { name: DATABASE_ADMIN_URL, value: "postgresql://postgres:postgres@postgres:5432/postgres" }
            - { name: DM_ALLOW_ORIGINS, value: "*" }
            - { name: DM_BINARIES_MODE, value: local }
            - { name: DM_STORE_ENROLL_LOCALLY, value: "true" }
            - { name: DM_TELEMETRY_ENABLED, value: "false" }
            - { name: DM_RELAY_ENABLED, value: "false" }
          volumeMounts: [{ name: data, mountPath: /data/content }]
      volumes: [{ name: data, emptyDir: {} }]
---
apiVersion: v1
kind: Service
metadata: { name: device-management-admin, namespace: $NS }
spec:
  selector: { app: device-management-admin }
  ports: [{ port: 80, targetPort: 3001 }]
---
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: { name: dm-route, namespace: $NS }
spec:
  parentRefs: [{ group: gateway.networking.k8s.io, kind: Gateway, name: api-gateway, namespace: envoy-gateway-system }]
  rules:
    - backendRefs: [{ kind: Service, name: device-management, port: 80 }]
      matches: [{ path: { type: PathPrefix, value: /bootstrap } }]
      filters: [{ type: URLRewrite, urlRewrite: { path: { type: ReplacePrefixMatch, replacePrefixMatch: / } } }]
    - backendRefs: [{ kind: Service, name: device-management, port: 80 }]
      matches: [{ path: { type: PathPrefix, value: /catalog } }]
    - backendRefs: [{ kind: Service, name: device-management-admin, port: 80 }]
      matches: [{ path: { type: PathPrefix, value: /admin } }]
EOF

info "waiting for postgres..."
kubectl -n "$NS" rollout status deploy/postgres --timeout=90s >/dev/null 2>&1
# Extra wait for postgres to accept connections (rollout ready != accepting TCP)
sleep 8

# Bootstrap schema
kubectl -n "$NS" delete configmap dm-schema --ignore-not-found >/dev/null 2>&1
kubectl -n "$NS" create configmap dm-schema --from-file=schema.sql="$ROOT_DIR/db/schema.sql" >/dev/null
kubectl -n "$NS" delete pod apply-schema --ignore-not-found >/dev/null 2>&1
kubectl -n "$NS" run apply-schema --restart=Never \
  --image=docker.io/<DOCKERHUB_NAMESPACE>/postgres:16-alpine \
  --overrides='{"spec":{"imagePullSecrets":[{"name":"regcred"}],"containers":[{"name":"s","image":"docker.io/<DOCKERHUB_NAMESPACE>/postgres:16-alpine","command":["sh","-c","until pg_isready -h postgres; do sleep 1; done && psql postgresql://postgres:postgres@postgres:5432/bootstrap -v ON_ERROR_STOP=1 -f /sql/schema.sql && echo SCHEMA_OK"],"volumeMounts":[{"name":"sql","mountPath":"/sql"}]}],"volumes":[{"name":"sql","configMap":{"name":"dm-schema"}}]}}' >/dev/null
# Wait for schema
for i in $(seq 1 20); do
  PHASE=$(kubectl -n "$NS" get pod apply-schema -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
  [ "$PHASE" = "Succeeded" ] && break
  [ "$PHASE" = "Failed" ] && break
  sleep 3
done
SCHEMA_LOG=$(kubectl -n "$NS" logs apply-schema 2>/dev/null | tail -3)
if echo "$SCHEMA_LOG" | grep -q "SCHEMA_OK\|already exists"; then
  ok "schema applied"
else
  warn "schema status: $SCHEMA_LOG"
fi
kubectl -n "$NS" delete pod apply-schema --ignore-not-found >/dev/null 2>&1
kubectl -n "$NS" delete configmap dm-schema --ignore-not-found >/dev/null 2>&1

info "restarting apps (ensure schema is loaded)..."
kubectl -n "$NS" rollout restart deploy/device-management deploy/device-management-admin >/dev/null 2>&1

info "waiting for app..."
kubectl -n "$NS" rollout status deploy/waf-proxy --timeout=60s >/dev/null 2>&1
kubectl -n "$NS" rollout status deploy/device-management --timeout=120s >/dev/null 2>&1
kubectl -n "$NS" rollout status deploy/device-management-admin --timeout=120s >/dev/null 2>&1
# Extra settle time for Envoy to pick up new endpoints
sleep 5
ok "all pods running"

# ── 4. Run E2E tests ─────────────────────────────
step "Running E2E test suite through WAF + Envoy Gateway"

kubectl -n "$NS" delete pod e2e-runner --ignore-not-found >/dev/null 2>&1
kubectl -n "$NS" run e2e-runner --restart=Never \
  --image=docker.io/<DOCKERHUB_NAMESPACE>/curl:8.10.1 \
  --overrides='{"spec":{"imagePullSecrets":[{"name":"regcred"}]}}' \
  -- sh -c '
WAF=waf-proxy:8080
C=$(mktemp)
P=0; F=0

ok()   { echo "  ✅ $1"; P=$((P+1)); }
fail() { echo "  ❌ $1"; F=$((F+1)); }

t_get() {
  S=$(curl -s -b $C -c $C -o /dev/null -w "%{http_code}" "http://$WAF$2")
  [ "$S" = "$3" ] && ok "$1 → $S" || fail "$1 → $S (expected $3)"
}

t_post_blocked() {
  S=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    -H "Sec-Fetch-Mode: navigate" -d "$3" "http://$WAF$2")
  [ "$S" = "403" ] && ok "$1 (native) → 403 BLOCKED" || fail "$1 (native) → $S (expected 403)"
}

t_post_fetch() {
  S=$(curl -s -b $C -c $C -o /tmp/b -D /tmp/h -w "%{http_code}" \
    -X POST -H "Sec-Fetch-Mode: cors" $3 "http://$WAF$2")
  [ "$S" = "$4" ] && ok "$1 (fetch) → $S" || { B=$(head -c 120 /tmp/b | tr "\n" " "); fail "$1 (fetch) → $S (expected $4) $B"; }
}

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  E2E: WAF + Envoy + device-management           ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

echo "── GET routes ──"
t_get "healthz" /bootstrap/healthz 200
t_get "admin dashboard" /admin/ 200
t_get "admin catalog list" /admin/catalog 200
t_get "admin catalog new form" /admin/catalog/new 200
t_get "public catalog" /catalog 200

echo ""
echo "── WAF blocking: native form POST ──"
t_post_blocked "POST /admin/catalog" /admin/catalog "slug=blocked&name=Blocked"
t_post_blocked "POST without header" /admin/catalog "slug=noheader"

echo ""
echo "── WAF pass: fetch() POST ──"
t_post_fetch "Create plugin" /admin/catalog \
  "-F slug=e2e-plugin -F name=E2E+Plugin -F device_type=libreoffice -F category=productivity -F publisher=DNUM -F visibility=public" 303

LOC=$(grep -i "^location:" /tmp/h | tr -d "\r" | sed "s/location: //i")
[ -n "$LOC" ] && t_get "Redirect after create" "$LOC" 200

echo ""
echo "── Admin operations via fetch ──"
t_post_fetch "Duplicate plugin" /admin/catalog/1/duplicate "" 303
t_post_fetch "Edit plugin" /admin/catalog/1/edit \
  "-F name=E2E+Updated -F description=Updated+via+E2E -F changelog=[]" 303

echo ""
echo "── Public API ──"
t_get "catalog JSON API" /bootstrap/catalog/api/plugins 200
t_get "catalog API status" /bootstrap/catalog/api/status 200
t_get "healthz" /bootstrap/healthz 200

echo ""
echo "══════════════════════════════════════════"
echo " Results: $P passed, $F failed"
echo "══════════════════════════════════════════"
[ "$F" -gt 0 ] && exit 1 || exit 0
'

# Wait for test pod
for i in $(seq 1 30); do
  PHASE=$(kubectl -n "$NS" get pod e2e-runner -o jsonpath='{.status.phase}' 2>/dev/null || echo "")
  case "$PHASE" in Succeeded|Failed) break ;; esac
  sleep 3
done

echo ""
kubectl -n "$NS" logs e2e-runner 2>/dev/null
EXIT_CODE=0
PHASE=$(kubectl -n "$NS" get pod e2e-runner -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
[ "$PHASE" != "Succeeded" ] && EXIT_CODE=1
kubectl -n "$NS" delete pod e2e-runner --ignore-not-found >/dev/null 2>&1

# cleanup is done by trap EXIT
exit $EXIT_CODE
