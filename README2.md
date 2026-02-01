# Device Management (FastAPI)

Remplacement de l'implémentation Nginx/Lua par une API FastAPI.

## Endpoints

- `GET /config/config.json` : retourne la configuration (dynamique via variables d'environnement)
- `POST|PUT /enroll` : enregistre un payload JSON (stockage local et/ou S3)
- `GET /healthz` : retourne l'état de santé (200 si OK, 412 si prerequis manquants)
- `GET /binaries/{path}` : sert des binaires stockés dans S3
  - mode `presign` (par défaut) : redirige vers une URL présignée
  - mode `proxy` : proxy/streaming via l'API (le client ne voit pas S3)

## Variables d'environnement (préfixe `DM_`)

### URL publique (utilisée dans config/config.json)
- `PUBLIC_BASE_URL=https://server.com`

Le fichier `config/config.json` supporte les placeholders `${VARNAME}` (ex: `${PUBLIC_BASE_URL}`).

### API / CORS
- `DM_ALLOW_ORIGINS="*"` ou liste CSV d'origines
- `DM_MAX_BODY_SIZE_MB=10`

### /config/config.json
- `DM_CONFIG_ENABLED=true`
- `DM_APP_ENV=dev`
- `DM_ENROLL_URL=/enroll`

### Stockage enroll
- `DM_STORE_ENROLL_LOCALLY=true`
- `DM_ENROLL_DIR=/data/enroll`
- `DM_STORE_ENROLL_S3=false`
- `DM_S3_BUCKET=...`
- `DM_S3_PREFIX_ENROLL=enroll/`

### Binaires S3
- `DM_S3_PREFIX_BINARIES=binaries/`
- `DM_BINARIES_MODE=presign` (ou `proxy`)
- `DM_PRESIGN_TTL_SECONDS=300`

### AWS
L'app utilise les mécanismes standards (IAM role, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, etc.)

## Charger les variables d'environnement et secrets

- Docker Compose : `.env` + `.env.secrets`
- Kubernetes : Helm (`values.yaml` → `env:` et `secrets:`)

## Lancer en local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8088
```

## Lancer via Docker

```bash
docker build -t device-management-fastapi .
docker run --rm -p 8088:8088 -e DM_APP_ENV=dev -v "$(pwd)/data:/data" device-management-fastapi
```

## Lancer via docker-compose

```bash
cp .env.example .env
cp .env.secrets.example .env.secrets
# Éditez .env et .env.secrets (PUBLIC_BASE_URL, S3, secrets...)
docker compose up --build
```

## Déploiement Kubernetes (Helm)

Le chart Helm est disponible dans `helm/device-management`.

Exemple d'installation :

```bash
helm upgrade --install device-management ./helm/device-management \
  --set env.PUBLIC_BASE_URL=https://server.com \
  --set env.DM_APP_ENV=prod
```

### Configuration / secrets via Helm

- Variables non sensibles : `values.yaml` → `env:`
- Secrets : `values.yaml` → `secrets:` ou bien `existingSecretName`
- Fichier `config.json` : `values.yaml` → `config.configJson`

Exemple de `values.yaml` minimal :

```yaml
env:
  PUBLIC_BASE_URL: https://server.com
  DM_APP_ENV: prod
secrets:
  TELEMETRY_SALT: "super-secret"
  TELEMETRY_KEY: "super-secret-key"
```
