# Device Management (FastAPI)

Backend de gestion de plugins pour les outils bureautiques (LibreOffice, Thunderbird).
Configuration centralisee, deploiement progressif, telemetrie et relay securise.

## Plugins supportes

| Plugin | device_name | device_type | Extension |
|--------|-------------|-------------|-----------|
| Assistant Mirai LibreOffice | `mirai-libreoffice` | libreoffice | .oxt |
| Matisse Thunderbird | `mirai-matisse` | matisse | .xpi |

Le `device_name` est l'identifiant universel du plugin. Il est utilise dans les URLs,
le catalogue, l'enrollment et le matching de configuration.

## Documentation

- `developer-readme.md` : guide operations (dev/infra)
- `consumer-readme.md` : integration client (PKCE, endpoints, cURL)
- `prompts/` : prompts executables pour l'IA (admin UI, deploy wizard, catalogue)

## Endpoints

### Configuration
- `GET /config/config.json` : configuration generique (dynamique via variables d'environnement)
- `GET /config/{device_name}/config.json` : configuration specifique au plugin
  - `device_name` : slug du catalogue (`mirai-libreoffice`, `mirai-matisse`) ou alias (`libreoffice`, `matisse`)
  - `?profile=dev|int|prod` : profil d'environnement

### Enrollment et relay
- `POST|PUT /enroll` : enregistrement d'un plugin (JSON payload)
- `GET /relay/authorize` : autorisation relay (interne, utilise par nginx)
- `/relay-assistant/{path}` : proxy vers le service relay-assistant (nginx)

### Telemetrie
- `GET /telemetry/token` : token Bearer court-duree pour la telemetrie
- `POST /telemetry/v1/traces` : relay telemetrie vers le collecteur upstream

### Binaires
- `GET /binaries/{path}` : binaires stockes en S3
  - `presign` (defaut) : redirection vers une URL pre-signee
  - `proxy` : streaming via l'API

### Sante
- `GET /healthz` : verification des dependances (DB, S3, stockage local)
- `GET /livez` : probe de liveness (toujours 200)

### Administration
- `GET /admin/` : tableau de bord (OIDC, groupe `admin-dm` requis)
- `GET /admin/deploy` : assistant de deploiement 1-2-3
- `GET /admin/catalog` : catalogue de plugins
- `GET /admin/communications` : campagnes de communication et sondages
- `GET /admin/devices` : liste des appareils enregistres
- `GET /admin/campaigns` : campagnes de deploiement (avance)
- `GET /admin/cohorts` : groupes de ciblage
- `GET /admin/flags` : feature flags
- `GET /admin/artifacts` : artefacts binaires
- `GET /admin/audit` : journal d'audit

## Architecture

```
app/
  main.py              # API FastAPI (config, enroll, relay, telemetrie, binaires)
  admin/
    router.py          # Admin UI (Jinja2 + HTMX)
    auth.py            # OIDC session + CSRF
    services/          # Couche service (DB)
    templates/         # Templates HTML
    static/            # CSS

config/
  libreoffice/         # Templates config LibreOffice (dev/int/prod)
  matisse/             # Templates config Matisse (dev/int/prod)

deploy/
  docker/              # Docker Compose (dev local)
  k8s/
    base/              # Manifests Kubernetes (base)
    overlays/          # Overlays par environnement (local, scaleway, dgx)

db/
  schema.sql           # Schema initial
  migrations/          # Migrations SQL incrementales

scripts/
  build-local.sh       # Build Docker arm64 (dev rapide)
  build-k8s.sh         # Build multi-arch amd64+arm64 + push registry
  k8s/                 # Scripts deploiement Kubernetes
```

## Variables d'environnement (`DM_` prefix)

### URL publique
- `PUBLIC_BASE_URL=https://server.com`

Les templates config supportent `${{VARNAME}}` (substitution au runtime).

### API / CORS
- `DM_ALLOW_ORIGINS="*"` ou liste CSV
- `DM_MAX_BODY_SIZE_MB=10`

### Configuration
- `DM_CONFIG_ENABLED=true`
- `DM_CONFIG_PROFILE=prod` (defaut, utilise si `?profile=` absent)
- `DM_APP_ENV=dev`

### Telemetrie
- `DM_TELEMETRY_ENABLED=true`
- `DM_TELEMETRY_TOKEN_TTL_SECONDS=300`
- `DM_TELEMETRY_TOKEN_SIGNING_KEY=...`
- `DM_TELEMETRY_REQUIRE_TOKEN=true`
- `DM_TELEMETRY_UPSTREAM_ENDPOINT=https://telemetry.minint.fr/v1/traces`

### Relay
- `DM_RELAY_ENABLED=true`
- `DM_RELAY_KEY_TTL_SECONDS=2592000`
- `DM_RELAY_ALLOWED_TARGETS_CSV=keycloak,config,llm,mcr-api,telemetry`
- `DM_RELAY_REQUIRE_KEY_FOR_SECRETS=true`

### Keycloak
- `KEYCLOAK_ISSUER_URL=https://sso.example.com/realms/myrealm`
- `KEYCLOAK_REALM=myrealm`
- `KEYCLOAK_CLIENT_ID=bootstrap-iassistant`

### LLM (analyse IA du catalogue)
- `LLM_BASE_URL=https://api.scaleway.ai/.../v1`
- `LLM_API_TOKEN=...`
- `DEFAULT_MODEL_NAME=deepseek-r1-distill-llama-70b`

### Stockage
- `DM_STORE_ENROLL_LOCALLY=true`
- `DM_STORE_ENROLL_S3=false`
- `DM_S3_BUCKET=bootstrap`
- `DM_BINARIES_MODE=presign` (ou `proxy`)
- `DATABASE_URL=postgresql://dev:dev@postgres:5432/bootstrap`

## Lancer en local

### Docker Compose (recommande)

```bash
cp .env.example .env
cp .env.secrets.example .env.secrets
cd deploy/docker
docker compose up --build
```

Services : DM (3001), relay-assistant (8088), postgres (5432), adminer (8080)

### Python direct

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 3001
```

## Build et deploiement

### Local (arm64, rapide)
```bash
./scripts/build-local.sh [tag]
docker compose -f deploy/docker/docker-compose.yml up -d
```

### Kubernetes (multi-arch amd64+arm64)
```bash
./scripts/build-k8s.sh 0.1.0-my-feature
./scripts/k8s/deploy.sh scaleway
```

### Profils Kubernetes
- `local` : `http://bootstrap.home`
- `scaleway` : `https://bootstrap.fake-domain.name`
- `dgx` : `https://internal-domain/bootstrap`

## Secure Relay Flow (PKCE -> enroll -> relay)

```mermaid
sequenceDiagram
  autonumber
  participant U as User
  participant P as Plugin
  participant KC as Keycloak
  participant DM as Device Management
  participant R as relay-assistant (nginx)
  participant API as Upstream APIs

  P->>DM: GET /config/mirai-libreoffice/config.json
  DM-->>P: public config (secrets scrubbed)

  U->>P: Click Login (PKCE)
  P->>KC: /auth (PKCE)
  KC-->>P: authorization code
  P->>KC: /token (code+verifier)
  KC-->>P: access_token (+refresh_token)

  P->>DM: POST /enroll (Authorization: Bearer access_token)
  DM-->>P: relayClientId + relayClientKey + expiry

  P->>DM: GET /config/mirai-libreoffice/config.json + X-Relay-Client/X-Relay-Key
  DM-->>P: config with secret values

  P->>R: /relay-assistant/keycloak/... + relay headers
  R->>DM: GET /relay/authorize?target=keycloak
  DM-->>R: 200/403
  R->>API: proxied request
  API-->>R: response
  R-->>P: response
```

### Modele de securite
- L'enrollment requiert un token PKCE valide.
- Les credentials relay sont generes cote serveur et tournes au re-enrollment.
- Les valeurs secretes de config ne sont retournees qu'avec des relay credentials valides.
- `relay-assistant` est un proxy nginx avec whitelist stricte de paths.
- Pas de forwarding libre : la cible est contrainte aux prefixes autorises.

## Auto-Update (deploiement progressif)

Le DM supporte le deploiement progressif par paliers (5% → 25% → 50% → 100%)
avec gating base sur un hash deterministe du device UUID.

```mermaid
sequenceDiagram
  autonumber
  participant C as Plugin
  participant DM as Device Management

  C->>DM: GET /config/mirai-libreoffice/config.json (+ version headers)
  DM-->>C: config + update directive (target version, artifact URL, checksum)
  C->>C: verify checksum + install
  C->>DM: telemetry: update_success/failure
```

L'admin UI propose un assistant **"Deploiement 1-2-3"** (`/admin/deploy`) :
1. Choisir le plugin et uploader le fichier
2. Definir la cible (tous, groupe de test, pourcentage)
3. Configurer le rythme (progressif ou patch urgent) et lancer

## Validation

```bash
./scripts/test-all.sh                    # Tous les tests
./scripts/k8s/validate-all.sh            # Manifests k8s
curl -sS http://localhost:3001/healthz   # Health check local
```
