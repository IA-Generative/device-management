#!/usr/bin/env sh
set -eu

ENV_FILE=".env"
SECRETS_FILE=".env.secrets"

if [ ! -f "$ENV_FILE" ]; then
  cat <<'EOT' > "$ENV_FILE"
# Public base URL used in config/config.json (via ${PUBLIC_BASE_URL})
PUBLIC_BASE_URL=http://localhost:3001
MODELNAME1=

# App settings
DM_APP_ENV=dev
DM_CONFIG_ENABLED=true
DM_CONFIG_PROFILE=dev
DM_ENROLL_URL=/enroll
DM_ALLOW_ORIGINS=*
DM_MAX_BODY_SIZE_MB=10
DM_PORT=3001

# Keycloak (local)
KEYCLOAK_ISSUER_URL=http://localhost:8080/realms/bootstrap
KEYCLOAK_REALM=bootstrap
KEYCLOAK_CLIENT_ID=device-management-plugin

# Local enroll storage
DM_STORE_ENROLL_LOCALLY=true
DM_ENROLL_DIR=/data/enroll

# Optional S3 (set DM_STORE_ENROLL_S3=true to enable)
DM_STORE_ENROLL_S3=false
DM_S3_BUCKET=bootstrap
DM_S3_PREFIX_ENROLL=enroll/
DM_S3_PREFIX_BINARIES=binaries/
DM_BINARIES_MODE=presign
DM_PRESIGN_TTL_SECONDS=300
DM_S3_ENDPOINT_URL=
AWS_REGION=

# Postgres
POSTGRES_DB=bootstrap
POSTGRES_USER=postgres
POSTGRES_PASSWORD=postgres
POSTGRES_PORT=5432

# Adminer
ADMINER_PORT=8080

# Database URL for app (local compose)
DATABASE_URL=postgresql://dev:dev@postgres:5432/bootstrap
DATABASE_ADMIN_URL=postgresql://postgres:postgres@postgres:5432/postgres

# scaleway
SCW_ACCESS_KEY=
SCW_SECRET_KEY=
SCW_DEFAULT_ORGANIZATION_ID=
SCW_DEFAULT_PROJECT_ID=


EOT
  echo "Created $ENV_FILE"
else
  echo "$ENV_FILE already exists, skipping"
fi

if [ ! -f "$SECRETS_FILE" ]; then
  cat <<'EOT' > "$SECRETS_FILE"
# Secrets (do not commit real values)
TOKENMODEL1=
TELEMETRY_SALT=
TELEMETRY_KEY=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_SESSION_TOKEN=
EOT
  echo "Created $SECRETS_FILE"
else
  echo "$SECRETS_FILE already exists, skipping"
fi
