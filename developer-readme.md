# Developer README

Guide operations pour le service Device Management (dev local + Kubernetes).

## Services

| Service | Port | Description |
|---------|------|-------------|
| device-management | 3001 | API FastAPI + Admin UI |
| relay-assistant | 8088 | Proxy nginx (Keycloak, LLM, MCR) |
| queue-worker | — | Worker asynchrone (telemetrie, jobs) |
| postgres | 5432 | Base de donnees |
| adminer | 8080 | Admin DB |

## Plugins geres

| device_name | device_type | Alias | Maturite |
|-------------|-------------|-------|----------|
| `mirai-libreoffice` | libreoffice | `libreoffice` | release |
| `mirai-matisse` | matisse | `matisse` | beta |

- `device_name` = identifiant universel (URL, catalogue, matching)
- `device_type` = detail interne (chargement template config)
- `alias` = retrocompatibilite (les anciens chemins fonctionnent toujours)

## Quick Start (Docker Compose)

```bash
cd deploy/docker
docker compose up -d --build
```

Verification :
```bash
curl -sS http://localhost:3001/healthz | python3 -m json.tool
curl -sS 'http://localhost:3001/config/mirai-libreoffice/config.json?profile=dev' | python3 -m json.tool
```

Admin UI : http://localhost:3001/admin/ (auto-login en dev)

## Pipeline de configuration

Quand un plugin appelle `/config/{x}/config.json?profile=dev` :

```
1. RESOLVE : x → (device_name, device_type, plugin_id, resolved_via)
   - slug "mirai-libreoffice" → match direct
   - alias "libreoffice" → lookup + LOG acces alias
   - inconnu → 400

2. TEMPLATE : charge config/{device_type}/config.{profile}.json

3. SUBSTITUTION : ${{VAR}} → valeurs env systeme

4. OVERRIDES DM : telemetrie, relay, etc.

5. OVERRIDES CATALOGUE : plugin_env_overrides (par plugin + env)

6. KEYCLOAK : injecte client_id + realm specifiques au plugin/env

7. ACCESS CONTROL : open | waitlist | keycloak_group

8. INJECTION : force device_name + config_path dans la reponse
   (meme si appel via alias → migration douce automatique)

9. SCRUB : secrets masques si pas de relay credentials

10. ENRICHMENT : campaigns, features, communications
```

Templates config sur disque (par device_type, inchanges) :
```
config/
  libreoffice/config.json, config.dev.json, config.int.json
  matisse/config.json, config.dev.json, config.int.json
```

## Base de donnees

### Schema unique consolide

Un seul fichier `db/schema.sql` contient tout le schema (plus de migrations incrementales).

```bash
# Reset complet (Scaleway)
kubectl -n bootstrap exec deploy/device-management -- python -c "
import psycopg2
conn = psycopg2.connect('postgresql://postgres:postgres@postgres:5432/bootstrap')
conn.autocommit = True
cur = conn.cursor()
cur.execute('DROP SCHEMA public CASCADE')
cur.execute('CREATE SCHEMA public')
with open('/app/db/schema.sql') as f: cur.execute(f.read())
cur.execute(\"SELECT COUNT(*) FROM pg_tables WHERE schemaname='public'\")
print(f'{cur.fetchone()[0]} tables creees')
conn.close()
"
```

### Tables principales

| Groupe | Tables |
|--------|--------|
| **Core** | `provisioning`, `device_connections`, `relay_clients`, `queue_jobs` |
| **Catalogue** | `plugins`, `plugin_aliases`, `plugin_versions`, `plugin_installations`, `plugin_env_overrides`, `plugin_waitlist`, `alias_access_log` |
| **Deploiement** | `artifacts`, `campaigns`, `campaign_device_status`, `cohorts`, `cohort_members` |
| **Features** | `feature_flags`, `feature_flag_overrides` |
| **Communications** | `communications`, `survey_responses`, `communication_acks` |
| **Keycloak** | `keycloak_clients`, `plugin_keycloak_clients` |
| **Telemetrie** | `device_telemetry_events` |
| **Audit** | `admin_audit_log` |

## Build et deploiement

### Docker local (arm64, rapide)

```bash
./scripts/build-local.sh [tag]
docker compose -f deploy/docker/docker-compose.yml up -d
```

### Kubernetes (multi-arch)

```bash
./scripts/build-k8s.sh 0.1.1-catalog    # build amd64+arm64 + push
./scripts/k8s/deploy.sh scaleway         # deploy

# Verifier (~12s)
kubectl -n bootstrap rollout status deployment/device-management
```

### Probes optimisees

| Service | Ready en | Strategie |
|---------|----------|-----------|
| device-management (x4) | 3-7s | startupProbe period=2s, maxSurge=2 |
| queue-worker (x2) | 2s | aucune probe |
| relay-assistant | 1s | readinessProbe period=3s |
| telemetry-relay | 11s | startupProbe period=2s |
| postgres | 3s | pg_isready, Recreate (PVC RWO) |
| adminer | 7s | readinessProbe period=3s |

Rollout complet 6 services : ~12s.

## Variables d'environnement

### Essentielles

| Variable | Exemple |
|----------|---------|
| `PUBLIC_BASE_URL` | `http://localhost:3001` |
| `DM_CONFIG_PROFILE` | `dev` |
| `DATABASE_URL` | `postgresql://dev:dev@postgres:5432/bootstrap` |
| `KEYCLOAK_ISSUER_URL` | `http://localhost:8082/realms/openwebui` |
| `KEYCLOAK_REALM` | `openwebui` |
| `KEYCLOAK_CLIENT_ID` | `bootstrap-iassistant` |

### LLM (catalogue IA)

| Variable | Description |
|----------|-------------|
| `LLM_BASE_URL` | Endpoint OpenAI-compatible |
| `LLM_API_TOKEN` | Token API |
| `DEFAULT_MODEL_NAME` | Modele par defaut |

## Health check

```bash
curl -sS http://localhost:3001/healthz
```
```json
{"type":"...","title":"OK","status":200,"detail":"All dependencies are healthy.",
 "checks":{"local_storage":{"status":"ok"},"s3":{"status":"ok"},"db":{"status":"ok"}}}
```

## Operations courantes

```bash
# Logs (sans probes)
kubectl -n bootstrap logs deploy/device-management --tail=50 | grep -v livez

# Redemarrer
kubectl -n bootstrap rollout restart deployment/device-management

# Tester un endpoint depuis le cluster
kubectl -n bootstrap exec deploy/device-management -- python -c "
import urllib.request; r = urllib.request.urlopen('http://localhost:3001/admin/')
print(r.status, r.read()[:100].decode())
"
```

## Profils Kubernetes

| Profil | URL | Commande |
|--------|-----|----------|
| local | `http://bootstrap.home` | `./scripts/k8s/deploy.sh local` |
| scaleway | `https://bootstrap.fake-domain.name` | `./scripts/k8s/deploy.sh scaleway` |
| dgx | `https://internal-domain/bootstrap` | `./scripts/k8s/deploy.sh dgx` |
