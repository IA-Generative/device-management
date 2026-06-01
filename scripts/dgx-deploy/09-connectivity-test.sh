#!/usr/bin/env bash
# scripts/dgx-deploy/09-connectivity-test.sh
# ---------------------------------------------------------------------------
# Test de connectivite DGX, sans dependance a un pod existant ni a kubectl exec.
# Lance un Job ephemere qui utilise curl (avec fallback wget) pour tester :
#   - DNS interne et public
#   - HTTP via proxy (registry, SSO, compte-rendu)
#   - HTTP direct (services .<INTERNAL_DOMAIN> et cluster internes si disponibles)
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ── Config (overridable via env) ──────────────────────────
NAMESPACE="${NAMESPACE:-bootstrap}"
JOB_NAME="connectivity-test-$(date +%s)"
PROXY="${PROXY:-http://<PROXY_IP>:3128}"
NO_PROXY="${NO_PROXY:-localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,.<INTERNAL_DOMAIN>,.svc,.svc.cluster.local}"
TIMEOUT_SEC="${TIMEOUT_SEC:-10}"
# Image avec curl ET wget, mirror sur DockerHub <DOCKERHUB_NAMESPACE> (compte authentifie
# via regcred → pas de rate limit).
IMAGE="${TEST_IMAGE:-docker.io/<DOCKERHUB_NAMESPACE>/curl:8.10.1}"
# Secret docker-registry pour pull (doit exister dans le namespace).
IMAGE_PULL_SECRET="${IMAGE_PULL_SECRET:-regcred}"

# Registry pour image pull (DockerHub depuis qu'on a basculé)
REG_NAME="DockerHub"
REG_HOST="index.docker.io"

# Use hostNetwork ? (utile si le proxy est sur localhost du node)
USE_HOST_NETWORK="${USE_HOST_NETWORK:-false}"

# ── Endpoints a tester ────────────────────────────────────
# Format : NOM|URL|TYPE
#   proxy   = via proxy corporate (domaines publics .gouv.fr, .scw.cloud)
#   direct  = direct, sans proxy (domaines internes .<INTERNAL_DOMAIN>)
#   cluster = service interne k8s (peut ne pas exister au premier run)
ENDPOINTS=(
  "SSO OIDC discovery|https://<SSO_HOSTNAME>/realms/mirai/.well-known/openid-configuration|proxy"
  "SSO JWKS|https://<SSO_HOSTNAME>/realms/mirai/protocol/openid-connect/certs|proxy"
  "Compte-Rendu|https://<COMPTERENDU_HOSTNAME>/|proxy"
  "DockerHub Registry v2|https://index.docker.io/v2/|proxy"
  "DockerHub image manifest|https://index.docker.io/v2/<DOCKERHUB_NAMESPACE>/curl/manifests/8.10.1|proxy"

  "LLM API <INTERNAL_DOMAIN>|https://<LLM_API_HOSTNAME>/v1/models|direct"

  "device-management cluster|http://device-management.${NAMESPACE}.svc.cluster.local:3001/health|cluster"
  "relay-assistant healthz|http://relay-assistant.${NAMESPACE}.svc.cluster.local:8080/healthz|cluster"
  "relay JWKS passthrough|http://relay-assistant.${NAMESPACE}.svc.cluster.local:8080/keycloak/protocol/openid-connect/certs|cluster"
)

# ── Generation du script de test embarque dans le Job ────
build_test_script() {
  cat <<'SCRIPT_HEADER'
#!/bin/sh
# Test runner — uses curl if available, fallback to wget.

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'
PASS=0
FAIL=0
WARN=0

HAS_CURL=0
HAS_WGET=0
command -v curl >/dev/null 2>&1 && HAS_CURL=1
command -v wget >/dev/null 2>&1 && HAS_WGET=1

# test_http <name> <url> <use_proxy> <timeout>
#   use_proxy = "yes" | "no"
test_http() {
  name="$1"
  url="$2"
  use_proxy="$3"
  timeout="$4"
  printf "%-42s " "$name"

  status=""
  body=""

  if [ "$HAS_CURL" = "1" ]; then
    # curl : -s silencieux, -k no-tls-verify, -w status+errormsg, --max-time
    if [ "$use_proxy" = "yes" ]; then
      out=$(curl -sk -o /tmp/body --connect-timeout 5 --max-time "$timeout" \
        -w "STATUS=%{http_code} CODE=%{exitcode} MSG=%{errormsg}" \
        -x "$https_proxy" "$url" 2>&1) || true
    else
      out=$(curl -sk -o /tmp/body --connect-timeout 5 --max-time "$timeout" \
        -w "STATUS=%{http_code} CODE=%{exitcode} MSG=%{errormsg}" \
        --noproxy '*' "$url" 2>&1) || true
    fi
    status=$(echo "$out" | sed -n 's/.*STATUS=\([0-9]*\).*/\1/p')
    err_code=$(echo "$out" | sed -n 's/.*CODE=\([0-9]*\).*/\1/p')
    err_msg=$(echo "$out" | sed -n 's/.*MSG=\(.*\)$/\1/p')
    body=$(head -c 200 /tmp/body 2>/dev/null)
    rm -f /tmp/body
  elif [ "$HAS_WGET" = "1" ]; then
    if [ "$use_proxy" = "yes" ]; then
      resp=$(wget -q -O- --no-check-certificate -S --timeout="$timeout" "$url" 2>&1)
    else
      resp=$(env -u https_proxy -u http_proxy -u HTTPS_PROXY -u HTTP_PROXY \
        wget -q -O- --no-check-certificate -S --timeout="$timeout" "$url" 2>&1)
    fi
    status=$(echo "$resp" | grep -i 'HTTP/' | tail -1 | awk '{print $2}')
    body=$(echo "$resp" | tail -c 200)
  else
    printf "${RED}NO-TOOL${NC} (neither curl nor wget)\n"
    FAIL=$((FAIL + 1))
    return
  fi

  # Verdict
  if [ -z "$status" ] || [ "$status" = "000" ]; then
    # Diagnostic curl exit codes : 5=cant resolve proxy, 6=cant resolve host,
    # 7=cant connect, 28=timeout, 35=ssl handshake, 56=recv failure
    case "${err_code:-}" in
      5)  printf "${RED}FAIL${NC} (proxy DNS unresolved)\n" ;;
      6)  printf "${RED}FAIL${NC} (host DNS unresolved)\n" ;;
      7)  printf "${RED}FAIL${NC} (cannot connect — proxy unreachable or refused)\n" ;;
      28) printf "${RED}FAIL${NC} (timeout after ${timeout}s)\n" ;;
      35) printf "${YELLOW}PARTIAL${NC} (proxy tunnel OK, upstream TLS failed — endpoint not serving valid cert)\n"
          WARN=$((WARN + 1))
          return ;;
      56) printf "${RED}FAIL${NC} (recv failure)\n" ;;
      *)  printf "${RED}FAIL${NC} (curl exit=${err_code:-?}: ${err_msg:-no detail})\n" ;;
    esac
    FAIL=$((FAIL + 1))
  elif [ "$status" -lt 500 ] 2>/dev/null; then
    printf "${GREEN}OK${NC} (HTTP %s)\n" "$status"
    PASS=$((PASS + 1))
  else
    printf "${RED}FAIL${NC} (HTTP %s)\n" "$status"
    [ -n "$body" ] && echo "  body: $(echo "$body" | head -c 120)"
    FAIL=$((FAIL + 1))
  fi
}

echo "============================================="
echo " DGX Connectivity Test"
echo " $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
echo "============================================="
echo ""
echo "Tools  : curl=$HAS_CURL  wget=$HAS_WGET"
echo "Proxy  : ${https_proxy:-<unset>}"
echo "NoProxy: ${no_proxy:-<unset>}"
echo ""

echo "--- DNS Resolution ---"
for host in <SSO_HOSTNAME> <COMPTERENDU_HOSTNAME> <LLM_API_HOSTNAME> index.docker.io; do
  printf "%-42s " "$host"
  if command -v getent >/dev/null 2>&1; then
    ip=$(getent hosts "$host" 2>/dev/null | awk '{print $1}' | head -1)
  elif command -v nslookup >/dev/null 2>&1; then
    ip=$(nslookup "$host" 2>/dev/null | awk '/^Address: / && !/#/{print $2; exit}')
  elif command -v host >/dev/null 2>&1; then
    ip=$(host "$host" 2>/dev/null | awk '/has address/{print $4; exit}')
  fi
  if [ -n "$ip" ]; then
    printf "${GREEN}OK${NC} (%s)\n" "$ip"
  else
    printf "${YELLOW}WARN${NC} (DNS public non resolu — proxy fera la resolution)\n"
    WARN=$((WARN + 1))
  fi
done
echo ""

SCRIPT_HEADER

  echo 'echo "--- Endpoints via proxy (Internet) ---"'
  for entry in "${ENDPOINTS[@]}"; do
    IFS='|' read -r name url type <<< "$entry"
    [ "$type" = "proxy" ] && echo "test_http \"$name\" \"$url\" yes $TIMEOUT_SEC"
  done

  echo 'echo ""'
  echo 'echo "--- Endpoints direct (.<INTERNAL_DOMAIN>) ---"'
  for entry in "${ENDPOINTS[@]}"; do
    IFS='|' read -r name url type <<< "$entry"
    [ "$type" = "direct" ] && echo "test_http \"$name\" \"$url\" no $TIMEOUT_SEC"
  done

  echo 'echo ""'
  echo 'echo "--- Services cluster internes (peuvent ne pas exister au 1er run) ---"'
  for entry in "${ENDPOINTS[@]}"; do
    IFS='|' read -r name url type <<< "$entry"
    [ "$type" = "cluster" ] && echo "test_http \"$name\" \"$url\" no $TIMEOUT_SEC"
  done

  cat <<'SCRIPT_FOOTER'

echo ""
echo "============================================="
echo " Results: $PASS passed, $FAIL failed, $WARN warnings"
echo "============================================="
[ "$FAIL" -gt 0 ] && exit 1
exit 0
SCRIPT_FOOTER
}

# ── Pre-flight (cote kubectl, hors Job) ──────────────────

echo "============================================="
echo " DGX Connectivity Test"
echo "============================================="
echo ""
echo "  Namespace : $NAMESPACE"
echo "  Job       : $JOB_NAME"
echo "  Image     : $IMAGE"
echo "  Proxy     : $PROXY"
echo "  HostNet   : $USE_HOST_NETWORK"
echo "  Timeout   : ${TIMEOUT_SEC}s"
echo ""

echo "--- Pre-flight: Image Pull Secret ---"
printf "%-42s " "Secret 'regcred' exists"
if kubectl -n "$NAMESPACE" get secret regcred >/dev/null 2>&1; then
  echo -e "\033[0;32mOK\033[0m"

  printf "%-42s " "regcred targets $REG_NAME"
  REGCRED=$(kubectl -n "$NAMESPACE" get secret regcred -o jsonpath='{.data.\.dockerconfigjson}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
  if echo "$REGCRED" | grep -q "$REG_HOST"; then
    echo -e "\033[0;32mOK\033[0m"
  else
    echo -e "\033[1;33mWARN\033[0m (regcred ne reference pas $REG_HOST)"
  fi
else
  echo -e "\033[1;33mWARN\033[0m (regcred absent — lance dumb-deploy.sh d'abord)"
fi
echo ""

# ── Build le script de test ──────────────────────────────

TEST_SCRIPT=$(build_test_script)
TEST_SCRIPT_B64=$(echo "$TEST_SCRIPT" | base64 | tr -d '\n')

# ── Definir le bloc hostNetwork si demande ───────────────
HOST_NETWORK_LINE=""
if [ "$USE_HOST_NETWORK" = "true" ]; then
  HOST_NETWORK_LINE="      hostNetwork: true"
fi

# ── Creer le Job ──────────────────────────────────────────

cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: connectivity-test
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    metadata:
      labels:
        app: connectivity-test
    spec:
      restartPolicy: Never
      imagePullSecrets:
        - name: ${IMAGE_PULL_SECRET}
${HOST_NETWORK_LINE}
      containers:
        - name: test
          image: ${IMAGE}
          command: ["sh", "-c"]
          args:
            - |
              echo "${TEST_SCRIPT_B64}" | base64 -d > /tmp/test.sh
              chmod +x /tmp/test.sh
              sh /tmp/test.sh
          env:
            - name: https_proxy
              value: "${PROXY}"
            - name: http_proxy
              value: "${PROXY}"
            - name: HTTPS_PROXY
              value: "${PROXY}"
            - name: HTTP_PROXY
              value: "${PROXY}"
            - name: no_proxy
              value: "${NO_PROXY}"
            - name: NO_PROXY
              value: "${NO_PROXY}"
          resources:
            requests:
              cpu: 50m
              memory: 32Mi
            limits:
              cpu: 200m
              memory: 128Mi
EOF

echo ""
echo "Job created: $JOB_NAME"
echo "Waiting for pod to start..."
sleep 2
kubectl -n "$NAMESPACE" get pods -l job-name="$JOB_NAME" -o wide 2>/dev/null || true

echo ""
echo "Waiting for completion (max 120s)..."
if kubectl -n "$NAMESPACE" wait --for=condition=complete "job/$JOB_NAME" --timeout=120s 2>/dev/null; then
  EXIT_CODE=0
else
  EXIT_CODE=1
fi

echo ""
echo "========= TEST RESULTS ========="
echo ""
kubectl -n "$NAMESPACE" logs "job/$JOB_NAME" 2>/dev/null || echo "(no logs yet)"
echo ""

if [ "$EXIT_CODE" -ne 0 ]; then
  echo "Job did not complete cleanly. Pod status:"
  kubectl -n "$NAMESPACE" get pods -l job-name="$JOB_NAME" -o wide 2>/dev/null || true
fi

echo ""
echo "Cleaning up job..."
kubectl -n "$NAMESPACE" delete job "$JOB_NAME" --ignore-not-found >/dev/null 2>&1
exit "$EXIT_CODE"
