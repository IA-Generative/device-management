# Rapport final - Architecture Postgres Queue

_Date_: 2026-03-07
_Repository_: `/Users/etiquet/Documents/GitHub/device-management`

## 1) Resume executif
- Une file de traitement Postgres robuste a ete integree dans le service device-management.
- Les API et workers sont maintenant separables (`DM_RUNTIME_MODE=api|worker|all`) pour permettre le scaling horizontal.
- Les deploiements Docker Compose et Kubernetes ont ete mis a jour avec un service/deployment worker dedie.
- Des endpoints operations queue ont ete ajoutes (`/ops/queue/health`, `/ops/queue/stats`) avec protection par token admin.
- Les tests validation, securite, charge et resilience distribuee sont implementes et passent.

## 2) Snapshot Git initial + rollback
- Tag snapshot cree avant modifications: `pre-postgres-queue-snapshot-20260307-052626`
- SHA associe: `f3cebdf4f89197cca4a91af38e8180bdf317d361`

Rollback rapide:
- Vers SHA snapshot: `git reset --hard f3cebdf4f89197cca4a91af38e8180bdf317d361`
- Vers tag snapshot: `git reset --hard pre-postgres-queue-snapshot-20260307-052626`

## 3) Fichiers modifies
- `.env.example`
- `app/main.py`
- `app/postgres_queue.py`
- `app/settings.py`
- `app/worker_main.py`
- `deploy-dgx/.env.example`
- `deploy-dgx/db-schema.sql`
- `deploy-dgx/docker-compose.yml`
- `deploy-dgx/kustomization.yaml`
- `deploy-dgx/manifests/20-device-management-deployment.yaml`
- `deploy-dgx/manifests/28-queue-worker-deployment.yaml`
- `deploy/k8s/base/kustomization.yaml`
- `deploy/k8s/base/manifests/20-device-management-deployment.yaml`
- `deploy/k8s/base/manifests/28-queue-worker-deployment.yaml`
- `infra-minimal/.env.example`
- `infra-minimal/db-schema.sql`
- `infra-minimal/docker-compose.yml`
- `tests/test_queue_load.py`
- `tests/test_queue_resilience_distributed.py`
- `tests/test_queue_security.py`
- `tests/test_queue_validation.py`

## 4) Architecture avant/apres
Avant:
```text
Plugin -> FastAPI device-management -> telemetry upstream (synchrone)
                              \-> endpoints existants
```

Apres:
```text
Plugin -> FastAPI (API pods) -> queue_jobs (Postgres)
                              -> workers dedies (N replicas)
                              -> telemetry upstream
                              -> dead-letter queue_job_dead_letters

Ops:
- /ops/queue/health
- /ops/queue/stats
```

Points clefs implementes:
- claim atomique via `FOR UPDATE SKIP LOCKED`
- lock TTL + reprise jobs stale
- retry backoff exponentiel + jitter
- dead-letter apres `max_attempts`
- idempotence via `dedupe_key` unique `(topic, dedupe_key)`

## 5) Topologie multi-AZ / multi-cluster recommandee
Cible:
- 1 cluster principal multi-AZ (API + workers autoscalables)
- Postgres principal + replicas synchrones/asynchrones selon AZ
- 1 cluster de secours multi-AZ (standby chaud)

Synchronisation inter-cluster recommande:
- intra-cluster: replication streaming PostgreSQL
- inter-cluster: replication logique (publication/subscription) sur tables fonctionnelles critiques
- idempotence applicative preservee via `dedupe_key`

Objectifs:
- RPO <= 30s
- RTO <= 5 min

Failover inter-cluster:
1. Detection (health DB + lag replication + probes API/worker)
2. Promotion du cluster secondaire
3. Reroutage trafic (LB/DNS)
4. Reprise workers + verification de convergence

## 6) Resultats de tests
### Validation fonctionnelle
Commande:
```bash
pytest -q tests/test_enroll.py tests/test_relay.py tests/test_telemetry.py tests/test_queue_validation.py
```
Resultat: `12 passed`

### Securite
Commande:
```bash
pytest -q tests/test_queue_security.py
```
Resultat: `5 passed`
Couvre:
- protection endpoints ops par token admin
- payload SQL-like sans execution
- dedupe par `X-Idempotency-Key`
- rejet payload telemetry surdimensionne

### Charge (smoke)
Commande:
```bash
pytest -q -s tests/test_queue_load.py
```
Resultat: `1 passed`
Mesures:
- avg_latency_ms: `0.63`
- throughput_rps: `1588.86`
- failure_rate_pct: `0.00`
- backlog: `0`

### Resilience distribuee (simulee)
Commande:
```bash
pytest -q tests/test_queue_resilience_distributed.py
```
Resultat: `4 passed`
Couvre:
- coupure AZ simulee + failover
- perte worker et reclamation lock apres TTL
- latence inter-cluster vs objectif RPO
- convergence post-reprise vs objectif RTO

### Suite complete
Commande:
```bash
pytest -q tests
```
Resultat: `22 passed`

## 7) Bugs trouves et corriges
- **Moyen**: incompatibilite environnement tests pydantic v1/v2 (`pydantic._internal` introuvable).
  - Correction: compatibilite dual-stack dans `app/settings.py` (fallback pydantic v1).
- **Faible**: test queue health non aligne avec securisation des endpoints ops.
  - Correction: tests mis a jour avec token admin.
- **Moyen**: anti-rejeu non explicite cote endpoint queue.
  - Correction: propagation `X-Idempotency-Key`/`X-Request-Id` vers `dedupe_key` queue.

## 8) Risques residuels et mitigation
- Warnings FastAPI `on_event` deprecie.
  - Mitigation: migrer vers lifespan handlers.
- Les tests de resilience distribuee sont des simulations, pas un test de chaos sur infra reelle multi-AZ.
  - Mitigation: ajouter tests de chaos en environnement preprod (network partitions, failover DB reel).
- Pas d'execution de benchmark longue duree sur vraie base Postgres sous charge concurrente forte.
  - Mitigation: campagne Locust/k6 + workers multiples + suivi lag replication.

## 9) Runbook deploiement / rollback
Deploiement local compose:
```bash
cd infra-minimal
docker compose up -d --build
```

Deploiement k8s (exemple base):
```bash
kubectl apply -k deploy/k8s/base
```

Verification:
```bash
curl -sS http://<host>/ops/queue/health -H 'X-Queue-Admin-Token: <token>'
curl -sS http://<host>/ops/queue/stats  -H 'X-Queue-Admin-Token: <token>'
```

Rollback code:
```bash
git reset --hard pre-postgres-queue-snapshot-20260307-052626
```

## 10) SLO/SLA cibles et observabilite
SLO recommandes:
- Disponibilite API enqueue: >= 99.9%
- Latence enqueue p95: < 100 ms
- Age max pending p95: < 60 s
- Lag replication inter-cluster p95: < 30 s
- Taux dead-letter: < 0.5% sur 15 min

Metriques a monitorer:
- `pending`, `processing`, `done`, `dead`, `stale_processing`
- `oldest_pending_age_seconds`
- taux retry / taux dead-letter
- lag replication inter-cluster
- temps de convergence apres failover

## 11) Commandes de reproduction
```bash
# 1) Snapshot securite
cd /Users/etiquet/Documents/GitHub/device-management
git tag pre-postgres-queue-snapshot-<timestamp>

# 2) Tests validation / securite / charge / resilience
pytest -q tests/test_enroll.py tests/test_relay.py tests/test_telemetry.py tests/test_queue_validation.py
pytest -q tests/test_queue_security.py
pytest -q -s tests/test_queue_load.py
pytest -q tests/test_queue_resilience_distributed.py
pytest -q tests
```

## 12) Addendum live Scaleway (version 0.0.4-unified-relay)
Correctifs appliques apres campagne live:
- securite ops queue: `/ops/queue/*` retourne `503` si `DM_QUEUE_ADMIN_TOKEN` non configure (plus d'acces ouvert implicite).
- healthz: check S3 execute uniquement quand S3 est requis (`DM_STORE_ENROLL_S3=true` ou binaries mode `presign/proxy`).

Validation locale post-correctifs:
```bash
pytest -q tests/test_queue_security.py tests/test_queue_validation.py tests/test_enroll.py tests/test_relay.py tests/test_telemetry.py tests/test_queue_resilience_distributed.py tests/test_queue_load.py
```
Resultat: `25 passed`.

Deploiement live:
- image push: `rg.fr-par.scw.cloud/funcscwnspricelessmontalcinhiacgnzi/device-management:0.0.4-unified-relay`
- rollout API: OK (3/3)
- rollout worker: blocage PVC `Multi-Attach` resolu en sequence `replicas=0 -> 1`.

Checks live post-deploiement:
- `GET /healthz`: OK (S3 `skipped`, DB `ok`)
- `GET /livez`: `200`
- `POST /enroll` sans token: `401`
- `POST /enroll` JSON invalide: `400`
- `GET /ops/queue/health` sans token admin: `503`

Charge live 5000 enroll (concurrency 100):
- Run precedent (0.0.3): `success=4862`, `errors=138`, `error_rate=2.76%`, `throughput=44.25 rps`
- Run courant (0.0.4): `success=4945`, `errors=55`, `error_rate=1.10%`, `throughput=42.37 rps`
- Statuts (0.0.4): `201=4945`, `502=12`, `504=43`
- Latence (0.0.4): `p50=452ms`, `p95=2965ms`, `p99=37250ms`, `max=59823ms`

Queue post-charge (0.0.4):
- pic observe juste apres run: `pending=3524`, `processing=13`, `dead=0`
- backlog resorbe ensuite: `pending=0`, `processing=0`, `dead=0`

Diagnostic principal sur erreurs 502/504:
- Cause dominante observee sur la campagne precedente: pod API unique instable (timeouts probes `/livez`, restart, erreurs ingress upstream timeout/reset/refused vers `100.64.3.236`).
- Apres redeploiement propre 0.0.4, les restarts API sont a `0` sur les 3 pods.

Optimisations recommandees (priorisees):
1. Passer `queue-worker` a `replicas: 2+` avec stockage partage RWX (ou suppression de dependance PVC) pour augmenter le debit de resorption.
2. Ajuster probes API pour charge forte (timeouts/readiness) et ajouter budget de disruption pour eviter perte de capacite en pic.
3. Ajouter HPA API sur CPU + latence et HPA worker sur backlog queue.
4. Ajouter metriques explicites `enqueue_duration`, `job_processing_duration`, `oldest_pending_age` et alertes seuil.
5. Garder les endpoints ops fermes par defaut: token admin obligatoire en prod + rotation periodique.

## 13) Addendum Docker + Scaleway (version 0.0.5-unified-relay)
Livraison additionnelle:
- endpoint `/metrics` Prometheus expose par l'API (`dm_queue_*`, `dm_metrics_scrape_success`, `dm_runtime_worker_active`).
- HPA Kubernetes actifs:
  - `device-management`: min `3`, max `12`, cibles CPU `70%` + memoire `80%`.
  - `queue-worker`: min `1`, max `6`, cible CPU `70%`.
- worker Kubernetes rendu horizontalisable: suppression de la dependance PVC RWO (plus de blocage `Multi-Attach`).

Resultats Scaleway (apres deploiement 0.0.5):
- health API: OK sur 3 pods (`0` restart)
- endpoint `/metrics`: `dm_metrics_scrape_success=1`, `dm_queue_available=1`
- tir charge 5000 enroll (concurrency 100):
  - `success=4947`, `errors=53`, `error_rate=1.06%`
  - `throughput=40.98 rps`
  - `status`: `201=4947`, `502=8`, `504=45`
- observation autoscaling:
  - HPA API monte a `5` replicas en pic
  - HPA worker monte a `6` replicas
  - backlog queue passe de `pending=1024` a `pending=0` en ~30s

Resultats Docker (infra-minimal):
- compose et variables mis a jour avec `DM_METRICS_ENABLED=true`.
- endpoint local `/metrics` accessible.
- limite locale observee pendant validation: `postgres` en `no space left on device`, empechant l'init schema queue (probleme d'environnement local, pas de regression fonctionnelle du code).
