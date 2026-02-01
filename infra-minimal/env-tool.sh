#!/usr/bin/env sh
set -eu

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT_DIR/infra-minimal/.env"
SECRETS_FILE="$ROOT_DIR/infra-minimal/.env.secrets"
ENV_EXAMPLE="$ROOT_DIR/infra-minimal/.env.example"
SECRETS_EXAMPLE="$ROOT_DIR/infra-minimal/.env.secrets.example"
BOOTSTRAP_SECRET="$ROOT_DIR/infra-minimal/bootstrap-secret.yaml"

cmd="${1:-all}"

init_files() {
  if [ ! -f "$ENV_FILE" ]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    echo "Created $ENV_FILE from example"
  else
    echo "$ENV_FILE already exists, skipping"
  fi

  if [ ! -f "$SECRETS_FILE" ]; then
    cp "$SECRETS_EXAMPLE" "$SECRETS_FILE"
    echo "Created $SECRETS_FILE from example"
  else
    echo "$SECRETS_FILE already exists, skipping"
  fi
}

check_alignment() {
  ROOT_DIR="$ROOT_DIR" python - <<'PY'
import os
import sys

root = os.environ.get("ROOT_DIR") or os.getcwd()
env_file = os.path.join(root, "infra-minimal", ".env")
secrets_file = os.path.join(root, "infra-minimal", ".env.secrets")
bootstrap_secret = os.path.join(root, "infra-minimal", "bootstrap-secret.yaml")

def parse_env(path):
  out = {}
  if not os.path.exists(path):
    return out
  with open(path, "r", encoding="utf-8") as f:
    for line in f:
      line = line.strip()
      if not line or line.startswith("#"):
        continue
      if "=" not in line:
        continue
      k, _ = line.split("=", 1)
      k = k.strip()
      if k:
        out[k] = True
  return out

def parse_stringdata_keys(path):
  keys = []
  if not os.path.exists(path):
    return keys
  in_stringdata = False
  with open(path, "r", encoding="utf-8") as f:
    for raw in f:
      line = raw.rstrip("\n")
      if not in_stringdata:
        if line.strip() == "stringData:":
          in_stringdata = True
        continue
      if line.strip() == "" or line.lstrip().startswith("#"):
        continue
      if not line.startswith("  "):
        break
      if ":" not in line:
        continue
      key = line.strip().split(":", 1)[0].strip()
      if key:
        keys.append(key)
  return keys

env_keys = set(parse_env(env_file))
secret_keys = set(parse_env(secrets_file))
bootstrap_keys = set(parse_stringdata_keys(bootstrap_secret))

if not bootstrap_keys:
  print("ERROR: bootstrap-secret.yaml stringData keys not found.", file=sys.stderr)
  sys.exit(1)

combined = env_keys | secret_keys
missing = sorted(k for k in bootstrap_keys if k not in combined)
extra = sorted(k for k in combined if k not in bootstrap_keys)

errors = 0
if missing:
  errors += 1
  print("Missing keys (present in bootstrap-secret.yaml but not in .env/.env.secrets):")
  for k in missing:
    print(f"  - {k}")

if extra:
  print("Extra keys (present in .env/.env.secrets but not in bootstrap-secret.yaml):")
  for k in extra:
    print(f"  - {k}")

def read_env_value(path, key):
  if not os.path.exists(path):
    return ""
  with open(path, "r", encoding="utf-8") as f:
    for line in f:
      if not line.strip().startswith(f"{key}="):
        continue
      return line.split("=", 1)[1].strip()
  return ""

public_base = read_env_value(env_file, "PUBLIC_BASE_URL")
if public_base and "localhost" not in public_base:
  print("WARNING: PUBLIC_BASE_URL in infra-minimal/.env should be localhost for local/docker.")
  print(f"  current: {public_base}")

if errors:
  sys.exit(1)

print("OK: .env/.env.secrets are aligned with bootstrap-secret.yaml")
PY
}

check_git_ignores() {
  if ! command -v git >/dev/null 2>&1; then
    return 0
  fi
  tracked=""
  for f in "$ENV_FILE" "$SECRETS_FILE" "$ROOT_DIR/.env" "$ROOT_DIR/.env.secrets" "$BOOTSTRAP_SECRET"; do
    if git -C "$ROOT_DIR" ls-files --error-unmatch "$f" >/dev/null 2>&1; then
      tracked="$tracked $f"
    fi
  done
  if [ -n "$tracked" ]; then
    echo "WARNING: the following secret files are tracked by git:"
    echo "$tracked" | tr ' ' '\n' | sed '/^$/d' | sed 's/^/  - /'
  fi
}

case "$cmd" in
  init)
    init_files
    ;;
  check)
    check_alignment
    check_git_ignores
    ;;
  all)
    init_files
    check_alignment
    check_git_ignores
    ;;
  *)
    echo "Usage: $0 [init|check|all]" >&2
    exit 1
    ;;
esac
