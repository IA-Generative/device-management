#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/lib-dgx.sh"

SETTINGS_FILE="${SETTINGS_FILE:-$ROOT_DIR/deploy-dgx/settings.yaml}"
NAMESPACE="${NAMESPACE:-}"
SECRET_NAME="${SECRET_NAME:-device-management-secrets}"
DM_DEPLOYMENT="${DM_DEPLOYMENT:-device-management}"
RELAY_DEPLOYMENT="${RELAY_DEPLOYMENT:-relay-assistant}"
DM_CONTAINER="${DM_CONTAINER:-device-management}"
TAG="${TAG:-}"
IMAGE_REF="${IMAGE_REF:-}"
RUN_SMOKE_TEST="${RUN_SMOKE_TEST:-1}"
FAIL_ON_TEST_KO="${FAIL_ON_TEST_KO:-1}"

RELAY_PROXY_TOKEN="${RELAY_PROXY_TOKEN:-}"
RELAY_SECRET_PEPPER="${RELAY_SECRET_PEPPER:-}"
RELAY_KEYCLOAK_UPSTREAM="${RELAY_KEYCLOAK_UPSTREAM:-}"
RELAY_LLM_UPSTREAM="${RELAY_LLM_UPSTREAM:-}"
RELAY_MCR_API_UPSTREAM="${RELAY_MCR_API_UPSTREAM:-}"

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Upgrade relay enrollment flow on DGX Kubernetes in an idempotent way:
1) patch relay keys in secret
2) apply relay/device-management manifests
3) optionally update device-management image
4) rollout checks
5) run enrollment flow checks (expected vs actual)

Options:
  --namespace <ns>                 Kubernetes namespace (default: from settings.yaml)
  --secret <name>                  Secret name (default: device-management-secrets)
  --tag <tag>                      Set device-management image tag on cluster
  --image <full-image-ref>         Set full image ref on cluster (overrides --tag)
  --proxy-token <value>            DM_RELAY_PROXY_SHARED_TOKEN (auto-generated if empty)
  --secret-pepper <value>          DM_RELAY_SECRET_PEPPER (auto-generated if empty)
  --keycloak-upstream <url>        RELAY_KEYCLOAK_UPSTREAM
  --llm-upstream <url>             RELAY_LLM_UPSTREAM
  --mcr-upstream <url>             RELAY_MCR_API_UPSTREAM
  --no-smoke                       Skip enrollment flow checks
  --no-fail-on-test-ko             Do not fail script if enrollment checks have KO
  -h, --help                       Show help

Examples:
  DGX_SKIP_CONTEXT_CONFIRM=1 ./deploy-dgx/scripts/upgrade-relay-flow.sh --tag 0.0.2-relay
  DGX_SKIP_CONTEXT_CONFIRM=1 ./deploy-dgx/scripts/upgrade-relay-flow.sh --image rg.../device-management:0.0.2-relay
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --namespace) NAMESPACE="${2:-}"; shift 2 ;;
    --secret) SECRET_NAME="${2:-}"; shift 2 ;;
    --tag) TAG="${2:-}"; shift 2 ;;
    --image) IMAGE_REF="${2:-}"; shift 2 ;;
    --proxy-token) RELAY_PROXY_TOKEN="${2:-}"; shift 2 ;;
    --secret-pepper) RELAY_SECRET_PEPPER="${2:-}"; shift 2 ;;
    --keycloak-upstream) RELAY_KEYCLOAK_UPSTREAM="${2:-}"; shift 2 ;;
    --llm-upstream) RELAY_LLM_UPSTREAM="${2:-}"; shift 2 ;;
    --mcr-upstream) RELAY_MCR_API_UPSTREAM="${2:-}"; shift 2 ;;
    --no-smoke) RUN_SMOKE_TEST=0; shift ;;
    --no-fail-on-test-ko) FAIL_ON_TEST_KO=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

require_cmd kubectl
require_cmd python3
require_cmd curl
confirm_kubectl_context

if [ -z "$NAMESPACE" ]; then
  NAMESPACE="$(namespace_from_settings "$SETTINGS_FILE")"
fi
export NAMESPACE SECRET_NAME

random_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 16
  else
    python3 - <<'PY'
import secrets
print(secrets.token_hex(16))
PY
  fi
}

read_secret_key() {
  local key="$1"
  local encoded
  encoded="$(kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" -o "jsonpath={.data.${key}}" 2>/dev/null || true)"
  if [ -z "$encoded" ]; then
    return 0
  fi
  python3 - "$encoded" <<'PY'
import base64, sys
raw = sys.argv[1].strip()
if not raw:
    print("")
    raise SystemExit(0)
try:
    print(base64.b64decode(raw).decode("utf-8"))
except Exception:
    print("")
PY
}

if [ -z "$RELAY_PROXY_TOKEN" ]; then
  RELAY_PROXY_TOKEN="$(read_secret_key DM_RELAY_PROXY_SHARED_TOKEN)"
fi
if [ -z "$RELAY_PROXY_TOKEN" ]; then
  RELAY_PROXY_TOKEN="relay-proxy-$(random_hex)"
fi
if [ -z "$RELAY_SECRET_PEPPER" ]; then
  RELAY_SECRET_PEPPER="$(read_secret_key DM_RELAY_SECRET_PEPPER)"
fi
if [ -z "$RELAY_SECRET_PEPPER" ]; then
  RELAY_SECRET_PEPPER="relay-pepper-$(random_hex)"
fi

if [ -z "$RELAY_KEYCLOAK_UPSTREAM" ]; then
  issuer="$(read_yaml_key "$SETTINGS_FILE" "keycloak_issuer_url")"
  realm="$(read_yaml_key "$SETTINGS_FILE" "keycloak_realm")"
  issuer="${issuer%/}"
  if [[ "$issuer" == */realms/* ]]; then
    RELAY_KEYCLOAK_UPSTREAM="$issuer"
  else
    RELAY_KEYCLOAK_UPSTREAM="${issuer}/realms/${realm}"
  fi
fi

if [ -z "$RELAY_LLM_UPSTREAM" ]; then
  RELAY_LLM_UPSTREAM="$(read_secret_key LLM_BASE_URL)"
fi
if [ -z "$RELAY_LLM_UPSTREAM" ]; then
  RELAY_LLM_UPSTREAM="https://api.gpu.minint.fr/v1"
fi

if [ -z "$RELAY_MCR_API_UPSTREAM" ]; then
  RELAY_MCR_API_UPSTREAM="$(read_secret_key RELAY_MCR_API_UPSTREAM)"
fi
if [ -z "$RELAY_MCR_API_UPSTREAM" ]; then
  RELAY_MCR_API_UPSTREAM="https://mcr-api.fake-domain.name"
fi

DM_TELEMETRY_ENABLED="$(read_secret_key DM_TELEMETRY_ENABLED)"
if [ -z "$DM_TELEMETRY_ENABLED" ]; then
  DM_TELEMETRY_ENABLED="true"
fi

DM_TELEMETRY_PUBLIC_ENDPOINT="$(read_secret_key DM_TELEMETRY_PUBLIC_ENDPOINT)"
if [ -z "$DM_TELEMETRY_PUBLIC_ENDPOINT" ]; then
  DM_TELEMETRY_PUBLIC_ENDPOINT="/telemetry/v1/traces"
fi

DM_TELEMETRY_AUTHORIZATION_TYPE="$(read_secret_key DM_TELEMETRY_AUTHORIZATION_TYPE)"
if [ -z "$DM_TELEMETRY_AUTHORIZATION_TYPE" ]; then
  DM_TELEMETRY_AUTHORIZATION_TYPE="$(read_secret_key TELEMETRY_AUTHTYPE)"
fi
if [ -z "$DM_TELEMETRY_AUTHORIZATION_TYPE" ]; then
  DM_TELEMETRY_AUTHORIZATION_TYPE="Bearer"
fi

DM_TELEMETRY_UPSTREAM_ENDPOINT="$(read_secret_key DM_TELEMETRY_UPSTREAM_ENDPOINT)"
if [ -z "$DM_TELEMETRY_UPSTREAM_ENDPOINT" ]; then
  DM_TELEMETRY_UPSTREAM_ENDPOINT="$(read_secret_key TELEMETRY_URL)"
fi
if [ -z "$DM_TELEMETRY_UPSTREAM_ENDPOINT" ]; then
  DM_TELEMETRY_UPSTREAM_ENDPOINT="https://telemetry.minint.fr/v1/traces"
fi

DM_TELEMETRY_UPSTREAM_AUTH_TYPE="$(read_secret_key DM_TELEMETRY_UPSTREAM_AUTH_TYPE)"
if [ -z "$DM_TELEMETRY_UPSTREAM_AUTH_TYPE" ]; then
  DM_TELEMETRY_UPSTREAM_AUTH_TYPE="$(read_secret_key TELEMETRY_AUTHTYPE)"
fi
if [ -z "$DM_TELEMETRY_UPSTREAM_AUTH_TYPE" ]; then
  DM_TELEMETRY_UPSTREAM_AUTH_TYPE="Bearer"
fi

DM_TELEMETRY_UPSTREAM_KEY="$(read_secret_key DM_TELEMETRY_UPSTREAM_KEY)"
if [ -z "$DM_TELEMETRY_UPSTREAM_KEY" ]; then
  DM_TELEMETRY_UPSTREAM_KEY="$(read_secret_key TELEMETRY_KEY)"
fi

DM_TELEMETRY_TOKEN_TTL_SECONDS="$(read_secret_key DM_TELEMETRY_TOKEN_TTL_SECONDS)"
if [ -z "$DM_TELEMETRY_TOKEN_TTL_SECONDS" ]; then
  DM_TELEMETRY_TOKEN_TTL_SECONDS="300"
fi

DM_TELEMETRY_TOKEN_SIGNING_KEY="$(read_secret_key DM_TELEMETRY_TOKEN_SIGNING_KEY)"
if [ -z "$DM_TELEMETRY_TOKEN_SIGNING_KEY" ]; then
  DM_TELEMETRY_TOKEN_SIGNING_KEY="telemetry-signing-$(random_hex)"
fi

DM_TELEMETRY_REQUIRE_TOKEN="$(read_secret_key DM_TELEMETRY_REQUIRE_TOKEN)"
if [ -z "$DM_TELEMETRY_REQUIRE_TOKEN" ]; then
  DM_TELEMETRY_REQUIRE_TOKEN="true"
fi

DM_TELEMETRY_MAX_BODY_SIZE_MB="$(read_secret_key DM_TELEMETRY_MAX_BODY_SIZE_MB)"
if [ -z "$DM_TELEMETRY_MAX_BODY_SIZE_MB" ]; then
  DM_TELEMETRY_MAX_BODY_SIZE_MB="2"
fi

if [ -z "$IMAGE_REF" ] && [ -n "$TAG" ]; then
  current_image="$(kubectl -n "$NAMESPACE" get deploy "$DM_DEPLOYMENT" -o jsonpath='{.spec.template.spec.containers[0].image}')"
  IMAGE_REF="${current_image%:*}:$TAG"
fi

echo "== Relay flow upgrade =="
echo "namespace:              $NAMESPACE"
echo "secret:                 $SECRET_NAME"
echo "dm deployment/container:$DM_DEPLOYMENT/$DM_CONTAINER"
echo "relay deployment:       $RELAY_DEPLOYMENT"
echo "relay keycloak upstream:$RELAY_KEYCLOAK_UPSTREAM"
echo "relay llm upstream:     $RELAY_LLM_UPSTREAM"
echo "relay mcr upstream:     $RELAY_MCR_API_UPSTREAM"
if [ -n "$IMAGE_REF" ]; then
  echo "target image:           $IMAGE_REF"
fi
echo

echo "[1/6] Patching relay keys in secret (expected: patched)"
python3 - \
  "$RELAY_PROXY_TOKEN" \
  "$RELAY_SECRET_PEPPER" \
  "$RELAY_KEYCLOAK_UPSTREAM" \
  "$RELAY_LLM_UPSTREAM" \
  "$RELAY_MCR_API_UPSTREAM" \
  "$DM_TELEMETRY_ENABLED" \
  "$DM_TELEMETRY_PUBLIC_ENDPOINT" \
  "$DM_TELEMETRY_AUTHORIZATION_TYPE" \
  "$DM_TELEMETRY_UPSTREAM_ENDPOINT" \
  "$DM_TELEMETRY_UPSTREAM_AUTH_TYPE" \
  "$DM_TELEMETRY_UPSTREAM_KEY" \
  "$DM_TELEMETRY_TOKEN_TTL_SECONDS" \
  "$DM_TELEMETRY_TOKEN_SIGNING_KEY" \
  "$DM_TELEMETRY_REQUIRE_TOKEN" \
  "$DM_TELEMETRY_MAX_BODY_SIZE_MB" <<'PY'
import json, subprocess, sys, os
(
    proxy_token,
    pepper,
    kc,
    llm,
    mcr,
    telemetry_enabled,
    telemetry_public_endpoint,
    telemetry_authorization_type,
    telemetry_upstream_endpoint,
    telemetry_upstream_auth_type,
    telemetry_upstream_key,
    telemetry_token_ttl_seconds,
    telemetry_token_signing_key,
    telemetry_require_token,
    telemetry_max_body_size_mb,
) = sys.argv[1:16]
ns = os.environ["NAMESPACE"]
secret = os.environ["SECRET_NAME"]
patch = {
    "stringData": {
        "DM_RELAY_ENABLED": "true",
        "DM_RELAY_KEY_TTL_SECONDS": "2592000",
        "DM_RELAY_ALLOWED_TARGETS_CSV": "keycloak,config,llm,mcr-api,telemetry",
        "DM_RELAY_REQUIRE_KEY_FOR_SECRETS": "true",
        "DM_RELAY_PROXY_SHARED_TOKEN": proxy_token,
        "DM_RELAY_SECRET_PEPPER": pepper,
        "RELAY_KEYCLOAK_UPSTREAM": kc,
        "RELAY_LLM_UPSTREAM": llm,
        "RELAY_MCR_API_UPSTREAM": mcr,
        "DM_TELEMETRY_ENABLED": telemetry_enabled,
        "DM_TELEMETRY_PUBLIC_ENDPOINT": telemetry_public_endpoint,
        "DM_TELEMETRY_AUTHORIZATION_TYPE": telemetry_authorization_type,
        "DM_TELEMETRY_UPSTREAM_ENDPOINT": telemetry_upstream_endpoint,
        "DM_TELEMETRY_UPSTREAM_AUTH_TYPE": telemetry_upstream_auth_type,
        "DM_TELEMETRY_UPSTREAM_KEY": telemetry_upstream_key,
        "DM_TELEMETRY_TOKEN_TTL_SECONDS": telemetry_token_ttl_seconds,
        "DM_TELEMETRY_TOKEN_SIGNING_KEY": telemetry_token_signing_key,
        "DM_TELEMETRY_REQUIRE_TOKEN": telemetry_require_token,
        "DM_TELEMETRY_MAX_BODY_SIZE_MB": telemetry_max_body_size_mb,
    }
}
subprocess.check_call([
    "kubectl", "-n", ns, "patch", "secret", secret,
    "--type", "merge", "-p", json.dumps(patch),
])
print("actual: patched")
PY

echo "[2/6] Applying manifests (expected: configured/created)"
kubectl -n "$NAMESPACE" apply -f "$ROOT_DIR/deploy-dgx/manifests/18-device-management-content-pvc.yaml"
kubectl -n "$NAMESPACE" apply -f "$ROOT_DIR/deploy-dgx/manifests/19-device-management-enroll-pvc.yaml"
kubectl -n "$NAMESPACE" apply -f "$ROOT_DIR/deploy-dgx/manifests/20-device-management-deployment.yaml"
kubectl -n "$NAMESPACE" apply -f "$ROOT_DIR/deploy-dgx/manifests/22-httproute.yaml"
kubectl -n "$NAMESPACE" apply -f "$ROOT_DIR/deploy-dgx/manifests/25-relay-assistant-configmap.yaml"
kubectl -n "$NAMESPACE" apply -f "$ROOT_DIR/deploy-dgx/manifests/26-relay-assistant-deployment.yaml"
kubectl -n "$NAMESPACE" apply -f "$ROOT_DIR/deploy-dgx/manifests/27-relay-assistant-service.yaml"

echo "[3/6] Updating device-management image (expected: new image set or skipped)"
if [ -n "$IMAGE_REF" ]; then
  kubectl -n "$NAMESPACE" set image "deploy/$DM_DEPLOYMENT" "$DM_CONTAINER=$IMAGE_REF"
  echo "actual: image set to $IMAGE_REF"
else
  echo "actual: skipped (no --tag/--image provided)"
fi

echo "[4/6] Rollout checks (expected: success)"
kubectl -n "$NAMESPACE" rollout status "deploy/$DM_DEPLOYMENT" --timeout=300s
kubectl -n "$NAMESPACE" rollout status "deploy/$RELAY_DEPLOYMENT" --timeout=300s

echo "[5/6] Resource checks (expected: relay deployment/service present)"
kubectl -n "$NAMESPACE" get deploy "$RELAY_DEPLOYMENT" -o wide
kubectl -n "$NAMESPACE" get svc relay-assistant -o wide

if [ "$RUN_SMOKE_TEST" = "1" ]; then
  echo "[6/6] Enrollment flow checks (expected vs actual)"
  kubectl -n "$NAMESPACE" port-forward svc/device-management 18081:80 >/tmp/pf_dm_upgrade.log 2>&1 &
  PF_PID=$!
  cleanup_pf() { kill "$PF_PID" >/dev/null 2>&1 || true; }
  trap cleanup_pf EXIT

  for i in $(seq 1 20); do
    if curl -sS "http://127.0.0.1:18081/livez" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done

  set +e
  python3 - "$RELAY_PROXY_TOKEN" <<'PY'
import base64, json, sys, urllib.request, urllib.error
BASE='http://127.0.0.1:18081'
proxy_token=sys.argv[1]
payload={'device_name':'libreoffice','plugin_uuid':'b9bdf6ad-3b1f-4f1a-9f07-4f8606c3fe5a'}

def mk_jwt(data):
    h={"alg":"none","typ":"JWT"}
    def e(o):
        return base64.urlsafe_b64encode(json.dumps(o,separators=(',',':')).encode()).decode().rstrip('=')
    return f"{e(h)}.{e(data)}.sig"

def req(method, path, headers=None, body=None):
    data=None
    if body is not None:
        data=json.dumps(body).encode('utf-8')
    r=urllib.request.Request(BASE+path,data=data,headers=headers or {})
    r.get_method=lambda:method
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.read().decode('utf-8',errors='ignore')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8',errors='ignore')
    except Exception as e:
        return -1, repr(e)

rows=[]
def add(step, expected, actual, ok):
    rows.append((step, expected, actual, ok))

st, body = req('GET','/config/libreoffice/config.json?profile=prod')
scrub=False
if st==200:
    try:
        scrub = json.loads(body).get('config',{}).get('llm_api_tokens','') == ''
    except Exception:
        pass
add('config public', 'HTTP 200 + llm_api_tokens vide', f'HTTP {st}, scrub={scrub}', st==200 and scrub)

st, body = req('POST','/enroll', {'Content-Type':'application/json'}, payload)
add('enroll sans token', 'HTTP 401', f'HTTP {st}', st==401)

jwt=mk_jwt({'email':'upgrade@example.local','exp':4102444800})
st, body = req('POST','/enroll', {'Content-Type':'application/json','Authorization':f'Bearer {jwt}'}, payload)
relay_id=''
relay_key=''
if st==201:
    try:
        j=json.loads(body)
        relay_id=j.get('relayClientId','')
        relay_key=j.get('relayClientKey','')
    except Exception:
        pass
add('enroll avec token', 'HTTP 201 + relay credentials', f'HTTP {st}, relayId={bool(relay_id)}, relayKey={bool(relay_key)}', st==201 and bool(relay_id) and bool(relay_key))

st, body = req('GET','/relay/authorize?target=keycloak', {'X-Relay-Client':relay_id,'X-Relay-Key':relay_key})
add('authorize sans proxy token', 'HTTP 403', f'HTTP {st}', st==403)

st, body = req('GET','/relay/authorize?target=keycloak', {'X-Relay-Client':relay_id,'X-Relay-Key':relay_key,'X-Relay-Proxy-Token':proxy_token})
add('authorize avec proxy token', 'HTTP 200', f'HTTP {st}', st==200)

print('\\nEnrollment flow result:') 
ko=0
for step,exp,act,ok in rows:
    s='OK' if ok else 'KO'
    print(f'- {step} | attendu: {exp} | reel: {act} | {s}')
    if not ok:
      ko += 1
print(f'KO_TOTAL={ko}')
sys.exit(1 if ko else 0)
PY
  smoke_rc=$?
  set -e
  if [ "$smoke_rc" -ne 0 ] && [ "$FAIL_ON_TEST_KO" = "1" ]; then
    echo "ERROR: enrollment flow checks have KO (see output above)." >&2
    exit 1
  fi
  if [ "$smoke_rc" -ne 0 ]; then
    echo "WARN: enrollment flow checks have KO but --no-fail-on-test-ko is active."
  fi
else
  echo "[6/6] Enrollment flow checks skipped (--no-smoke)"
fi

echo
echo "Upgrade relay flow completed."
