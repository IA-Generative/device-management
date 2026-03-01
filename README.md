# Device Management (FastAPI)

Replacement of the Nginx/Lua implementation with a FastAPI API.

## Documentation
- `developer-readme.md`: operations guide (dev/infra)
- `consumer-readme.md`: client integration (PKCE, endpoints, cURL)

## Endpoints

- `GET /config/config.json`: returns configuration (dynamic via environment variables)
- `GET /config/<device>/config.json`: device-specific configuration (matisse, libreoffice, chrome, edge, firefox, misc)
- `POST|PUT /enroll`: records a JSON payload (local storage and/or S3)
- `GET /healthz`: returns health status (200 if OK, 412 if prerequisites missing)
- `GET /binaries/{path}`: serves binaries stored in S3
  - `presign` mode (default): redirects to a presigned URL
  - `proxy` mode: proxy/streaming via the API (client does not see S3)

## Environment variables (`DM_` prefix)

### Public URL (used in config/config.json)
- `PUBLIC_BASE_URL=https://server.com`

The `config/config.json` file supports placeholders `${VARNAME}` (e.g. `${PUBLIC_BASE_URL}`).

### API / CORS
- `DM_ALLOW_ORIGINS="*"` or CSV list of origins
- `DM_MAX_BODY_SIZE_MB=10`

### /config/config.json
- `DM_CONFIG_ENABLED=true`
- `DM_APP_ENV=dev`
- `DM_ENROLL_URL=/enroll`

### Enroll storage
- `DM_STORE_ENROLL_LOCALLY=true`
- `DM_ENROLL_DIR=/data/enroll`
- `DM_STORE_ENROLL_S3=false`
- `DM_S3_BUCKET=...`
- `DM_S3_PREFIX_ENROLL=enroll/`

### S3 binaries
- `DM_S3_PREFIX_BINARIES=binaries/`
- `DM_BINARIES_MODE=presign` (or `proxy`)
- `DM_PRESIGN_TTL_SECONDS=300`

### AWS
The app uses standard mechanisms (IAM role, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, etc.)

## Load environment variables and secrets

- Docker Compose: `.env` + `.env.secrets`
- Kubernetes: Helm (`values.yaml` → `env:` and `secrets:`)

## TODO (Enrollment)

Goal: secure enrollment with **PKCE**, enable **silent provisioning** (refresh token), and **secure parameter retrieval** in applications.

### 1) PKCE authentication (public client)
- Create a **public** Keycloak client with mandatory PKCE.
- Disable ROPC (Direct Access Grants).
- Strict redirect URL (localhost + allowed port).

### 2) Application enrollment
- The plugin retrieves the token via PKCE.
- Checks the token’s `email` field (and `email_verified` if available).
- Stores the refresh token in the system vault (Keychain/SecretService/Windows CredMan).

### 3) Silent provisioning
- Renew `access_token` via `refresh_token` without user interaction.
- If refresh fails → force re-auth.

### 4) Settings and configuration
- Fetch config via `/config/<device>/config.json`.
- Use `dm_bootstrap_url` to point to the source (prod vs dev).
- Keep secrets server-side (not in the plugin).

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8088
```

## Run with Docker

```bash
docker build -t device-management-fastapi .
docker run --rm -p 8088:8088 -e DM_APP_ENV=dev -v "$(pwd)/data:/data" device-management-fastapi
```

## Run with docker-compose

```bash
cp .env.example .env
cp .env.secrets.example .env.secrets
# Edit .env and .env.secrets (PUBLIC_BASE_URL, S3, secrets...)
docker compose up --build
```

## Kubernetes deployment (Helm)

The Helm chart is available in `helm/device-management`.

Example install:

```bash
helm upgrade --install device-management ./helm/device-management \
  --set env.PUBLIC_BASE_URL=https://server.com \
  --set env.DM_APP_ENV=prod
```

### Configuration / secrets via Helm

- Non-sensitive variables: `values.yaml` → `env:`
- Secrets: `values.yaml` → `secrets:` or `existingSecretName`
- `config.json` file: `values.yaml` → `config.configJson`

Minimal `values.yaml` example:

```yaml
env:
  PUBLIC_BASE_URL: https://server.com
  DM_APP_ENV: prod
secrets:
  TELEMETRY_SALT: "super-secret"
  TELEMETRY_KEY: "super-secret-key"
```

## TODO (Cloud Pi Native)

- Convert all Kubernetes manifests into a **Helm chart** (single entry point, centralized values, environment profiles).
- Externalize secrets in an **environment vault** compliant with **Cloud Pi Native** (www.cloud-pi-native.fr):
  - avoid plaintext secrets in Git,
  - define a rotation and access policy (least privilege),
  - inject secrets via native mechanisms (external-secrets / CSI / vault provider).
