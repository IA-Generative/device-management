#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/lib-dgx.sh"

SETTINGS_FILE="${1:-$ROOT_DIR/deploy-dgx/settings.yaml}"
ROLLOUT_TIMEOUT_SECONDS="${ROLLOUT_TIMEOUT_SECONDS:-180}"

if [ ! -f "$SETTINGS_FILE" ]; then
  echo "ERROR: missing settings file $SETTINGS_FILE" >&2
  exit 1
fi

require_cmd kubectl
confirm_kubectl_context

NAMESPACE="$(namespace_from_settings "$SETTINGS_FILE")"
DM_BINARIES_MODE="$(read_yaml_key "$SETTINGS_FILE" "dm_binaries_mode")"
HOSTNAME="$(read_yaml_key "$SETTINGS_FILE" "hostname")"
APP_PATH_PREFIX="$(read_yaml_key "$SETTINGS_FILE" "app_path_prefix")"
ADMINER_PATH_PREFIX="$(read_yaml_key "$SETTINGS_FILE" "adminer_path_prefix")"
FILEBROWSER_PATH_PREFIX="$(read_yaml_key "$SETTINGS_FILE" "filebrowser_path_prefix")"
EXTERNAL_SCHEME="${EXTERNAL_SCHEME:-https}"
EXTERNAL_INSECURE="${DGX_TEST_EXTERNAL_INSECURE:-0}"

APP_PATH_PREFIX="${APP_PATH_PREFIX%/}"
ADMINER_PATH_PREFIX="${ADMINER_PATH_PREFIX%/}"
FILEBROWSER_PATH_PREFIX="${FILEBROWSER_PATH_PREFIX%/}"
[ -z "$APP_PATH_PREFIX" ] && APP_PATH_PREFIX="/"
[ -z "$ADMINER_PATH_PREFIX" ] && ADMINER_PATH_PREFIX="/adminer"
[ -z "$FILEBROWSER_PATH_PREFIX" ] && FILEBROWSER_PATH_PREFIX="/files"

echo "== DGX Smoke Tests =="
echo "Namespace: $NAMESPACE"
echo "Binaries mode (settings): ${DM_BINARIES_MODE:-<unknown>}"
echo

echo "[1/4] Rollout status"
kubectl -n "$NAMESPACE" rollout status deployment/postgres --timeout="${ROLLOUT_TIMEOUT_SECONDS}s"
kubectl -n "$NAMESPACE" rollout status deployment/device-management --timeout="${ROLLOUT_TIMEOUT_SECONDS}s"
kubectl -n "$NAMESPACE" rollout status deployment/adminer --timeout="${ROLLOUT_TIMEOUT_SECONDS}s"
kubectl -n "$NAMESPACE" rollout status deployment/filebrowser --timeout="${ROLLOUT_TIMEOUT_SECONDS}s"

echo
echo "[2/4] Services and endpoints"
for svc in postgres device-management adminer filebrowser; do
  kubectl -n "$NAMESPACE" get svc "$svc" >/dev/null
  endpoints="$(kubectl -n "$NAMESPACE" get endpoints "$svc" -o jsonpath='{.subsets[*].addresses[*].ip}' 2>/dev/null || true)"
  if [ -z "${endpoints// }" ]; then
    echo "ERROR: service '$svc' has no ready endpoint in namespace '$NAMESPACE'" >&2
    exit 1
  fi
  echo "OK: service '$svc' has endpoint(s): $endpoints"
done

echo
echo "[3/4] Device-management in-pod functional tests"
SMOKE_JOB="dm-smoke-$(date +%s)"
cleanup_smoke_job() {
  kubectl -n "$NAMESPACE" delete job "$SMOKE_JOB" --ignore-not-found >/dev/null 2>&1 || true
}
trap cleanup_smoke_job EXIT

cat <<EOF | kubectl -n "$NAMESPACE" apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${SMOKE_JOB}
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      imagePullSecrets:
        - name: regcred
      containers:
        - name: smoke
          image: docker.io/etiquet/device-management:0.0.1
          imagePullPolicy: IfNotPresent
          env:
            - name: DM_BINARIES_MODE
              value: "${DM_BINARIES_MODE:-}"
          command:
            - python
            - -c
            - |
              import json
              import os
              import socket
              import sys
              import urllib.error
              import urllib.request

              failures = []


              def _ok(name: str) -> None:
                  print(f"OK: {name}")


              def _fail(name: str, detail: str) -> None:
                  failures.append(f"{name}: {detail}")
                  print(f"ERROR: {name}: {detail}")


              def check_http(name: str, url: str, *, expect_json: bool = False, allowed_error_codes=None) -> None:
                  allowed_error_codes = allowed_error_codes or set()
                  req = urllib.request.Request(url, headers={"Accept": "application/json"})
                  try:
                      with urllib.request.urlopen(req, timeout=5) as resp:
                          body = resp.read()
                          if expect_json:
                              json.loads(body.decode("utf-8"))
                          _ok(name)
                          return
                  except urllib.error.HTTPError as exc:
                      if exc.code in allowed_error_codes:
                          _ok(f"{name} (HTTP {exc.code} accepted)")
                          return
                      _fail(name, f"HTTP {exc.code}")
                      return
                  except Exception as exc:  # noqa: BLE001
                      _fail(name, repr(exc))
                      return


              def check_postgres_tcp() -> None:
                  name = "postgres tcp connect (postgres:5432)"
                  try:
                      sock = socket.create_connection(("postgres", 5432), timeout=3)
                      sock.close()
                      _ok(name)
                  except Exception as exc:  # noqa: BLE001
                      _fail(name, repr(exc))


              def check_png(url: str) -> None:
                  name = "device-management /binaries/test/ok.png"
                  try:
                      with urllib.request.urlopen(url, timeout=5) as resp:
                          sig = resp.read(8)
                      if sig != b"\x89PNG\r\n\x1a\n":
                          _fail(name, f"invalid PNG signature: {sig!r}")
                          return
                      _ok(name)
                  except Exception as exc:
                      _fail(name, repr(exc))


              check_http("device-management /livez", "http://device-management:80/livez", expect_json=True)
              check_http("device-management /config/matisse/config.json", "http://device-management:80/config/matisse/config.json", expect_json=True)
              check_http("device-management /config/libreoffice/config.json", "http://device-management:80/config/libreoffice/config.json", expect_json=True)

              check_http("adminer service", "http://adminer:8080/")
              check_http("filebrowser service", "http://filebrowser:80/", allowed_error_codes={401, 403})
              check_postgres_tcp()

              mode = os.getenv("DM_BINARIES_MODE", "").strip().lower()
              if mode == "local":
                  check_http("device-management /binaries/test/test.json", "http://device-management:80/binaries/test/test.json", expect_json=True)
                  check_png("http://device-management:80/binaries/test/ok.png")
              else:
                  print(f"INFO: skipping local-binaries checks because DM_BINARIES_MODE={mode!r}")

              if failures:
                  print("\nSmoke test failed:")
                  for item in failures:
                      print(f"- {item}")
                  sys.exit(1)

              print("\nAll smoke checks passed.")
EOF

if ! kubectl -n "$NAMESPACE" wait --for=condition=complete --timeout="${ROLLOUT_TIMEOUT_SECONDS}s" "job/${SMOKE_JOB}" >/dev/null 2>&1; then
  echo "ERROR: smoke job failed or timeout. Logs:" >&2
  kubectl -n "$NAMESPACE" logs "job/${SMOKE_JOB}" --tail=-1 || true
  exit 1
fi

kubectl -n "$NAMESPACE" logs "job/${SMOKE_JOB}" --tail=-1

echo
echo "[4/4] HTTPRoute objects"
kubectl -n "$NAMESPACE" get httproute device-management-route >/dev/null
echo "OK: HTTPRoute device-management-route found."

if [ "${DGX_TEST_EXTERNAL:-0}" = "1" ]; then
  echo
  echo "[5/5] External route checks (jump host -> gateway)"
  require_cmd curl

  curl_opts=(-sS -o /dev/null -w "%{http_code}")
  if [ "$EXTERNAL_INSECURE" = "1" ]; then
    curl_opts+=(-k)
  fi

  check_external_http() {
    local name="$1"
    local url="$2"
    shift 2
    local expected_codes=("$@")
    local code
    code="$(curl "${curl_opts[@]}" "$url" || true)"
    for c in "${expected_codes[@]}"; do
      if [ "$code" = "$c" ]; then
        echo "OK: $name -> HTTP $code ($url)"
        return 0
      fi
    done
    echo "ERROR: $name -> HTTP $code ($url), expected ${expected_codes[*]}" >&2
    exit 1
  }

  APP_BASE="${EXTERNAL_SCHEME}://${HOSTNAME}${APP_PATH_PREFIX}"
  ADMINER_BASE="${EXTERNAL_SCHEME}://${HOSTNAME}${ADMINER_PATH_PREFIX}"
  FILEBROWSER_BASE="${EXTERNAL_SCHEME}://${HOSTNAME}${FILEBROWSER_PATH_PREFIX}"

  check_external_http "device-management /livez" "$APP_BASE/livez" 200
  check_external_http "device-management /config/matisse/config.json" "$APP_BASE/config/matisse/config.json" 200
  check_external_http "device-management /config/libreoffice/config.json" "$APP_BASE/config/libreoffice/config.json" 200
  check_external_http "adminer route" "$ADMINER_BASE/" 200 301 302
  check_external_http "filebrowser route" "$FILEBROWSER_BASE/" 200 301 302 401 403

  echo
  echo "External route checks: SUCCESS"
fi

echo
echo "DGX smoke tests: SUCCESS"
