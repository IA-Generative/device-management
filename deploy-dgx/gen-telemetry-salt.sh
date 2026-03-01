#!/usr/bin/env sh
set -eu

# Generates a random salt and optionally updates deploy-dgx/.env.secrets
SALT=$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
)

SECRETS_FILE=".env.secrets"

if [ -f "$SECRETS_FILE" ]; then
  if rg -q "^TELEMETRY_SALT=" "$SECRETS_FILE" 2>/dev/null; then
    sed -i.bak "s/^TELEMETRY_SALT=.*/TELEMETRY_SALT=$SALT/" "$SECRETS_FILE"
    rm -f "${SECRETS_FILE}.bak"
  else
    printf '\nTELEMETRY_SALT=%s\n' "$SALT" >> "$SECRETS_FILE"
  fi
fi

echo "$SALT"
