# Developer README

Guide operations pour le service Device Management (dev local + Kubernetes).

## Vue d'ensemble

### Services

| Service | Port | Description |
|---------|------|-------------|
| device-management | 3001 | API FastAPI + Admin UI |
| relay-assistant | 8088 | Proxy nginx (Keycloak, LLM, MCR) |
| queue-worker | - | Worker asynchrone (telemetrie, jobs) |
| postgres | 5432 | Base de donnees |
| adminer | 8080 | Admin DB |

### Plugins geres

| device_name | device_type | Alias |
|-------------|-------------|-------|
| `mirai-libreoffice` | libreoffice | `libreoffice` |
| `mirai-matisse` | matisse | `matisse` |

Le `device_name` est l'identifiant universel. Le `device_type` est un detail
interne (selection du template config). Les alias assurent la retrocompatibilite.

## Quick Start (Docker Compose)

```bash
cd deploy/docker
cp .env .env.bak  # backup
docker compose up -d --build
```

Verification :
```bash
curl -sS http://localhost:3001/healthz | python3 -m json.tool
curl -sS 'http://localhost:3001/config/mirai-libreoffice/config.json?profile=dev' | python3 -m json.tool
```

Admin UI : http://localhost:3001/admin/ (auto-login en dev)

## Templates de configuration

Organisation sur disque (par `device_type`) :
```
config/
  config.json                    # Config generique (prod)
  config.dev.json                # Config generique (dev)
  libreoffice/
    config.json                  # LibreOffice prod
    config.dev.json              # LibreOffice dev
    config.int.json              # LibreOffice integration
  matisse/
    config.json                  # Matisse/Thunderbird prod
    config.dev.json              # Matisse dev
    config.int.json              # Matisse integration
```

Resolution :
1. `/config/mirai-libreoffice/config.json?profile=dev`
2. Lookup `mirai-libreoffice` → plugin catalogue → `device_type=libreoffice`
3. Charge `config/libreoffice/config.dev.json`
4. Substitue `${{VARNAME}}` avec les variables d'environnement
5. Applique les overrides DM (telemetrie, relay, etc.)
6. Applique les overrides catalogue (par environnement)
7. Scrub les secrets si pas de relay credentials

Fallback alias : `/config/libreoffice/config.json` → resolu via alias → meme resultat.

## Variables d'environnement

Definies dans `deploy/docker/.env` et `deploy/docker/.env.secrets`.

### Essentielles

| Variable | Description | Exemple |
|----------|-------------|---------|
| `PUBLIC_BASE_URL` | URL publique du service | `http://localhost:3001` |
| `DM_APP_ENV` | Environnement | `dev` |
| `DM_CONFIG_PROFILE` | Profil par defaut | `dev` |
| `DM_PORT` | Port HTTP | `3001` |
| `DATABASE_URL` | PostgreSQL (app) | `postgresql://dev:dev@postgres:5432/bootstrap` |
| `KEYCLOAK_ISSUER_URL` | Issuer Keycloak | `http://localhost:8082/realms/openwebui` |
| `KEYCLOAK_REALM` | Realm | `openwebui` |
| `KEYCLOAK_CLIENT_ID` | Client ID | `bootstrap-iassistant` |

### LLM (catalogue IA)

| Variable | Description |
|----------|-------------|
| `LLM_BASE_URL` | Endpoint LLM (OpenAI-compatible) |
| `LLM_API_TOKEN` | Token API |
| `DEFAULT_MODEL_NAME` | Modele par defaut |

## Base de donnees

### Schema initial + migrations

```bash
# Local (docker compose — auto au demarrage)
# K8s (manuel) :
kubectl -n bootstrap exec deploy/device-management -- python -c "
import psycopg2, glob
conn = psycopg2.connect('postgresql://postgres:postgres@postgres:5432/bootstrap')
conn.autocommit = True
cur = conn.cursor()
with open('/app/db/schema.sql') as f: cur.execute(f.read())
for mig in sorted(glob.glob('/app/db/migrations/*.sql')):
    with open(mig) as f: cur.execute(f.read())
cur.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO dev')
cur.execute('GRANT USAGE,SELECT,UPDATE ON ALL SEQUENCES IN SCHEMA public TO dev')
print('OK')
conn.close()
"
```

### Tables principales

| Table | Description |
|-------|-------------|
| `provisioning` | Enregistrements d'enrollment |
| `device_connections` | Audit des connexions |
| `relay_clients` | Credentials relay par client |
| `queue_jobs` | File d'attente asynchrone |
| `campaigns` | Campagnes de deploiement |
| `cohorts` / `cohort_members` | Groupes de ciblage |
| `artifacts` | Binaires de plugins |
| `feature_flags` / `feature_flag_overrides` | Feature toggles |
| `plugins` / `plugin_versions` | Catalogue de plugins |
| `plugin_installations` | Tracking des installations |
| `communications` / `survey_responses` | Campagnes de communication |
| `admin_audit_log` | Journal d'audit admin |

## Build et deploiement

### Docker local (arm64, rapide)

```bash
./scripts/build-local.sh [tag]
docker compose -f deploy/docker/docker-compose.yml up -d
```

### Kubernetes (multi-arch)

```bash
# Build amd64+arm64 et push vers la registry Scaleway
./scripts/build-k8s.sh 0.1.1-catalog

# Deployer sur un profil
./scripts/k8s/deploy.sh scaleway

# Verifier le rollout (~10s)
kubectl -n bootstrap rollout status deployment/device-management
```

### Probes optimisees

| Service | Ready en | Probe |
|---------|----------|-------|
| device-management | 3-7s | startupProbe period=2s |
| queue-worker | 2s | aucune (worker) |
| relay-assistant | 1s | readinessProbe period=3s |
| telemetry-relay | 11s | startupProbe period=2s |
| postgres | 3s | pg_isready, strategy=Recreate |

Rollout complet des 6 services : ~12s.

## Diagramme de sequence

```mermaid
sequenceDiagram
    participant Client
    participant API
    participant DB
    participant S3

    Client->>API: GET /config/mirai-libreoffice/config.json?profile=dev
    API->>DB: Lookup plugin (slug ou alias)
    API->>API: Charge template + substitution + overrides
    API-->>Client: config JSON

    Client->>API: POST /enroll (Bearer token)
    API->>DB: Upsert provisioning + relay_clients
    API-->>Client: relayClientId + relayClientKey

    Client->>API: POST /telemetry/v1/traces (Bearer token)
    API->>DB: Enqueue job
    API-->>Client: 202 Accepted

    Client->>API: GET /binaries/{path}
    alt presign
        API->>S3: Generate presigned URL
        API-->>Client: 302 redirect
    else proxy
        API->>S3: GetObject
        API-->>Client: 200 stream
    end
```

## Health Check

```bash
curl -sS http://localhost:3001/healthz
```

```json
{
  "type": "https://example.com/problems/dependency-check",
  "title": "OK",
  "status": 200,
  "detail": "All dependencies are healthy.",
  "checks": {
    "local_storage": {"status": "ok"},
    "s3": {"status": "ok"},
    "db": {"status": "ok"}
  }
}
```

## Operations courantes

### Voir les logs (sans probes)

```bash
kubectl -n bootstrap logs deploy/device-management --tail=50 | grep -v livez
```

### Redemarrer un service

```bash
kubectl -n bootstrap rollout restart deployment/device-management
```

### Executer une migration

```bash
kubectl -n bootstrap exec deploy/device-management -- python -c "
import psycopg2
conn = psycopg2.connect('postgresql://postgres:postgres@postgres:5432/bootstrap')
conn.autocommit = True
cur = conn.cursor()
with open('/app/db/migrations/007_aliases_env_keycloak.sql') as f:
    cur.execute(f.read())
print('OK')
conn.close()
"
```
