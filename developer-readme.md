# Developer README

This document is for developers operating the Device Management service in local Docker and Kubernetes.

## Overview
The service exposes:
- `/healthz` for dependency checks (S3 + DB) with `application/problem+json`.
- `/config/config.json` (and `?profile=dev|prod|int`) for client configuration.
- `/config/<device>/config.json` for device-specific configuration (device: matisse, libreoffice, chrome, edge, firefox, misc).
- `/binaries/{path}` to serve binaries from S3:
  - `presign` mode: 302 redirect to a presigned S3 URL.
  - `proxy` mode: the API streams the object directly.

## Quick Start (Local)
1) Fill env files:
- `infra-minimal/.env` (non-secret defaults)
- `infra-minimal/.env.secrets` (secrets)

Helper script:
```
./infra-minimal/env-tool.sh
```
This generates missing files from examples and checks alignment with `bootstrap-secret.yaml`.

Validation script:
```
./infra-minimal/validate-all.sh
```
It runs env alignment checks, starts local compose, and performs basic HTTP checks.

2) Run:
```
docker compose -f infra-minimal/docker-compose.yml up -d --build
```

3) Check:
```
curl -sS -H 'Accept: application/problem+json' http://localhost:3001/healthz
curl -sS http://localhost:3001/config/config.json | python -c 'import json,sys; print(json.load(sys.stdin).get("updateUrl"))'
```

## Configuration Files
Configuration is templated and read from `config/`:
- `config/config.json` (prod)
- `config/config.dev.json` (dev)
- `config/config.int.json` (int)
Device overrides:
- `config/<device>/config.json`
- `config/<device>/config.dev.json`
- `config/<device>/config.int.json`

Placeholders:
- `${{VARNAME}}` or `${VARNAME}` are replaced by env values at runtime.

Profiles:
- `DM_CONFIG_PROFILE=prod|dev|int` (default: prod)
- `GET /config/config.json?profile=dev|prod|int`
- `GET /config/<device>/config.json?profile=dev|prod|int`

## Environment Variables (Core)
Set in `infra-minimal/.env` and `infra-minimal/.env.secrets`, and in Kubernetes `bootstrap-secret.yaml`.

S3 (binaries and enroll):
- `DM_S3_ENDPOINT_URL=https://s3.fr-par.scw.cloud`
- `DM_S3_BUCKET=bootstrap`
- `DM_S3_PREFIX_BINARIES=binaries/`
- `DM_S3_PREFIX_ENROLL=enroll/`
- `AWS_REGION=fr-par`
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`

Binary mode:
- `DM_BINARIES_MODE=presign|proxy`

DB:
- `DATABASE_URL=postgresql://dev:dev@postgres:5432/bootstrap`
- `DATABASE_ADMIN_URL=postgresql://postgres:postgres@postgres:5432/postgres`

Telemetry:
- `TELEMETRY_URL`, `TELEMETRY_KEY`, `TELEMETRY_SALT`, `TELEMETRY_AUTHTYPE`

Models:
- `MODELNAME1`, `MODELNAME2`, `MODELNAME3`, `TOKENMODEL1/2/3`

## Sequence Diagram
```mermaid
sequenceDiagram
    participant Client
    participant API
    participant S3
    participant DB

    Client->>API: GET /config/config.json
    API->>API: Load template + env substitution
    API-->>Client: config JSON

    Client->>API: GET /binaries/{path}
    alt presign
        API->>S3: Generate presigned URL
        API-->>Client: 302 Location: presigned URL
        Client->>S3: GET object
        S3-->>Client: 200 object
    else proxy
        API->>S3: GetObject
        S3-->>API: object stream
        API-->>Client: 200 object stream
    end

    Client->>API: POST /enroll
    API->>DB: Log device_connections
    API-->>Client: 201 ok
```

## Health Check (Dependencies)
`GET /healthz` returns `application/problem+json` with HTTP 200.
Example (healthy):
```
{"type":"https://example.com/problems/dependency-check","title":"OK","status":200,"detail":"All dependencies are healthy.","checks":{"local_storage":{"status":"ok"},"s3":{"status":"ok"},"db":{"status":"ok"}}}
```

## Kubernetes (Namespace: bootstrap)
Apply resources:
```
kubectl -n bootstrap apply -f infra-minimal/bootstrap-secret.yaml
kubectl -n bootstrap apply -f infra-minimal/bootstrap-configmap-config.yaml
kubectl -n bootstrap apply -f infra-minimal/bootstrap-configmap-config-dev.yaml
kubectl -n bootstrap apply -f infra-minimal/bootstrap-configmap-config-int.yaml
kubectl -n bootstrap apply -f infra-minimal/bootstrap-app.yaml
kubectl -n bootstrap rollout restart deploy/device-management
kubectl -n bootstrap rollout status deploy/device-management
```

Check env in pod:
```
kubectl -n bootstrap exec -it deploy/device-management -- env | grep -E 'AWS_|DM_S3_'
```

## Docker Image Build + Push
```
set -a
source infra-minimal/.env.secrets
set +a
TAG=0.0.1 ./infra-minimal/build-push.sh
```

## Common Operations
Switch binaries mode:
- Set `DM_BINARIES_MODE=presign` or `proxy` in `bootstrap-secret.yaml` (and local `.env`)
- Redeploy.

Verify presign URL:
```
curl -sS -o /dev/null -D - https://bootstrap.fake-domain.name/binaries/matisse/evolution.png
```

Proxy should return 200 without redirect:
```
curl -sS -o /dev/null -D -L https://bootstrap.fake-domain.name/binaries/matisse/evolution.png
```
