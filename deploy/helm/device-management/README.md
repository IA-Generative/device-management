# device-management

Helm chart for **device-management**: distribution/configuration/telemetry
server for office-suite extensions. One container image, four roles
(`api` / `admin` / `worker` / `telemetryRelay`) driven by `DM_RUNTIME_MODE`,
plus a schema-migration Job that runs as a Helm hook.

This chart is the **only supported deployment path** for this application —
there is no companion deploy script. Every environment-specific need is
either a values key, a Helm hook, or one of the one-time `kubectl` commands
documented below (never an ad hoc script). It translates the reference
plain-Kubernetes manifests that used to be maintained alongside a set of
imperative deploy scripts in the private ops repo — see "Known quirks" below
for the deliberate differences from those manifests.

## Components

| Component | Deployment role | Replicas (default) | Notes |
|---|---|---|---|
| `api` | `DM_RUNTIME_MODE=api` | 4 | Main FastAPI app: enrollment, binaries/config distribution, relay authorize, queue producer. Stateless (S3-backed storage). |
| `admin` | `DM_RUNTIME_MODE=admin` | 1 | Back-office UI. Owns a writable content PVC; `Recreate` strategy. |
| `worker` | `DM_RUNTIME_MODE=worker` (`python -m app.worker_main`) | 2 | Queue consumer. Liveness via heartbeat-file freshness (no HTTP endpoint). |
| `telemetryRelay` | `DM_RUNTIME_MODE=api` (implicit), isolated config | 1 | Second instance of the same image, dedicated to telemetry ingestion so its traffic/config never shares blast radius with `api`. |
| `migration` | n/a (Job) | — | `alembic upgrade head`, runs as a `pre-install,pre-upgrade` hook before any Deployment rolls out. Self-sufficient on a fully empty database — see "First install" below. |
| `postgres.internal` | n/a (dev only) | 1 | Disabled by default. See install matrix. |

## Install matrix

### Production

External Postgres, external S3 (or S3-compatible), external Keycloak.
`postgres.internal.enabled` must stay `false` (the default).

```bash
helm upgrade --install device-management deploy/helm/device-management \
  -f my-prod-values.yaml \
  --namespace device-management --create-namespace
```

Start from `deploy/helm/examples/values-prod-example.yaml` (placeholders
only — copy it, fill in real hostnames, and keep the copy out of version
control; store site-specific values files in your private ops repo,
gitignored, next to a `*.example` template, same convention the old
manifest overlays used).

Required before install, at minimum:
- `image.registry` / `image.repository` / `image.tag` (or rely on `appVersion`).
- `config.keycloak.issuerUrl` / `realm` / `clientId`, `config.auth.jwksUrl`.
- `config.storage.bucket` (S3) plus AWS credentials.
- The runtime Secret (see "Secret provisioning" below), referenced via
  `secrets.existingSecret`.
- `secrets.values.DATABASE_URL` / `DATABASE_ADMIN_URL` (if using
  `secrets.existingSecret`, the equivalent keys in that Secret) pointing at
  your externally managed Postgres.

### Dev / demo

Internal Postgres, S3 fallback via a local MinIO (not bundled by this
chart — point `config.storage.s3EndpointUrl` at your own MinIO instance, or
set `config.storage.storeEnrollS3=false` /
`config.storage.storeEnrollLocally=true` to fall back to ephemeral local
storage on `api`/`worker` — add an `extraVolume`/`extraVolumeMount` if you
need it to survive restarts).

```bash
helm upgrade --install device-management deploy/helm/device-management \
  --set postgres.internal.enabled=true \
  --set secrets.values.POSTGRES_PASSWORD=dev \
  --set secrets.values.DATABASE_URL="postgresql://postgres:dev@RELEASE-device-management-postgres:5432/bootstrap" \
  --set secrets.values.DATABASE_ADMIN_URL="postgresql://postgres:dev@RELEASE-device-management-postgres:5432/bootstrap" \
  --set config.keycloak.issuerUrl=http://keycloak.example.invalid \
  --namespace device-management-dev --create-namespace
```

`postgres.internal` is a single, non-HA Postgres Deployment for
development/demo only — never enable it in production.

## Secret provisioning

The only imperative, one-time step this chart requires is getting real
credentials into the cluster — Helm should not be the system of record for
production secrets. Two supported paths:

**Recommended (production): a Secret managed outside Helm.**
Create it once with `kubectl` (or your secrets-management tool of choice —
external-secrets, sealed-secrets...), then point the chart at it:

```bash
kubectl create secret generic device-management-secrets \
  --namespace device-management \
  --from-literal=ADMIN_SESSION_SECRET="$(openssl rand -hex 32)" \
  --from-literal=DM_CONFIG_SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=DM_RELAY_SECRET_PEPPER="$(openssl rand -hex 32)" \
  --from-literal=DM_RELAY_PROXY_SHARED_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=DM_QUEUE_ADMIN_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=DM_TELEMETRY_TOKEN_SIGNING_KEY="$(openssl rand -hex 32)" \
  --from-literal=DM_TELEMETRY_UPSTREAM_KEY="<telemetry-collector-key>" \
  --from-literal=TELEMETRY_KEY="<content-template-token>" \
  --from-literal=TELEMETRY_SALT="$(openssl rand -hex 16)" \
  --from-literal=ADMIN_OIDC_CLIENT_SECRET="" \
  --from-literal=LLM_API_TOKEN="<llm-api-token>" \
  --from-literal=AWS_ACCESS_KEY_ID="<aws-access-key-id>" \
  --from-literal=AWS_SECRET_ACCESS_KEY="<aws-secret-access-key>" \
  --from-literal=AWS_SESSION_TOKEN="" \
  --from-literal=POSTGRES_PASSWORD="" \
  --from-literal=DATABASE_URL="postgresql://app_user:<password>@db.example.invalid:5432/device_management" \
  --from-literal=DATABASE_ADMIN_URL="postgresql://admin_user:<password>@db.example.invalid:5432/device_management" \
  --from-literal=DB_ADMIN_PASSWORD=""
```

Then set `secrets.existingSecret=device-management-secrets` (`--set` flag
or in your values file). The full key list is documented key-by-key in
`values.yaml` under `secrets.values` — every key the chart creates when
`secrets.create=true` must exist in your own Secret too if you go this
route, since every Deployment references them individually
(`secretKeyRef`), not as a whole-object `envFrom`.

**Dev/demo only: let the chart create the Secret.** Leave
`secrets.existingSecret` empty and pass real values via `--set` or a
gitignored values file (`secrets.create: true`, the default) — see the
warning `helm install`/`upgrade` prints in `NOTES.txt` when this path is
active.

## First install (empty database)

No separate schema-bootstrap step is needed. The migration Job
(`alembic upgrade head`) is self-sufficient on a completely empty Postgres
database: migration `001` executes the full `db/schema.sql` inline
(`CREATE EXTENSION IF NOT EXISTS pgcrypto/citext`, all tables, guarded with
`IF NOT EXISTS`/`CREATE OR REPLACE` throughout), and migrations `002`/`003`
are purely additive on top. Running the chart's `pre-install` hook against
a fresh database is the entire bootstrap procedure — there is no
`schema.sql` step to run separately, and none of the Deployments start
before the hook Job succeeds.

The one requirement this puts on you: `DATABASE_ADMIN_URL` must be a role
that can `CREATE EXTENSION` (extension creation has no privilege-fallback
in the SQL). On managed Postgres where even your admin role can't do that
(e.g. AWS RDS restricts it to `rds_superuser`), pre-create the `pgcrypto`
and `citext` extensions out-of-band before the first install — everything
else in the schema only needs ordinary DDL rights.

```bash
kubectl create namespace device-management
kubectl create secret generic device-management-secrets \
  --namespace device-management --from-literal=...   # see "Secret provisioning"
helm install device-management deploy/helm/device-management \
  -f my-prod-values.yaml --namespace device-management
kubectl -n device-management logs job/device-management-migrate   # verify
```

## Upgrade

```bash
helm upgrade device-management deploy/helm/device-management \
  -f my-prod-values.yaml --namespace device-management
```

- The migration Job (`helm.sh/hook: pre-install,pre-upgrade`) runs before any
  Deployment is touched; `helm.sh/hook-delete-policy: before-hook-creation`
  keeps the previous run around until the new one succeeds, so
  `kubectl logs job/<release>-device-management-migrate` after a failed
  upgrade still shows the last attempt.
- `image.tag` defaults to `.Chart.AppVersion`. Bump both together: this
  chart's `Chart.yaml` `appVersion` should track the application's `VERSION`
  file at the repo root — there is no automated sync yet (see repo task
  backlog: CI wiring for chart/image publishing).
- Changing `admin.persistence.accessModes` after first install does not
  resize/migrate the underlying PVC — Kubernetes does not support that.
  Recreate the PVC (and restore content) if you need to change access mode.
- Scaling `api`/`worker` replicas is always safe (S3-backed storage, no RWO
  contention). `admin` is designed for a single replica; do not scale it
  without first making its content volume ReadWriteMany.

## Rollback

```bash
helm rollback device-management [REVISION] --namespace device-management
```

`helm rollback` reverts Deployments/ConfigMap/Secret (if chart-managed) to
the previous release's rendered manifests, but it does **not** run the
migration hook backwards — Alembic migrations in this app are additive/
forward-only (no `downgrade()` bodies are relied upon), so rolling back the
chart does not undo a schema change. In practice:
- Rolling back to a revision with the **same** schema (no migration ran
  between the two) is safe and immediate.
- Rolling back across a migration boundary means the application is now
  older than the schema. Only do this once you've confirmed the migration
  in question is backward-compatible (additive columns/tables are; anything
  that dropped or renamed a column is not) — check the migration file under
  `alembic/versions/` before rolling back across it.
- There is no automatic schema-rollback Job. If you must revert a schema
  change, write and run the down-migration manually
  (`alembic downgrade <revision>`) against `DATABASE_ADMIN_URL` before or
  after `helm rollback`, depending on the change.

## Airgap install

The chart and image are pulled once on a connected host, mirrored into the
airgapped registry, then installed from there — no deploy script, no
on-site internet access required.

```bash
# 1. On a connected host: pull the chart (OCI) and the image, save both.
helm pull oci://registry.example.invalid/charts/device-management --version 0.1.0
docker pull registry.example.invalid/device-management:0.8.1
docker save registry.example.invalid/device-management:0.8.1 -o device-management-image.tar

# 2. Transfer device-management-0.1.0.tgz + device-management-image.tar
#    (and any base images your values reference, e.g. the internal-postgres
#    image if postgres.internal.enabled=true) to the airgapped network.

# 3. On the airgapped side: load and re-push to your internal registry.
docker load -i device-management-image.tar
docker tag registry.example.invalid/device-management:0.8.1 \
  internal-registry.example.invalid/device-management:0.8.1
docker push internal-registry.example.invalid/device-management:0.8.1

# 4. Install from the local chart archive, pointing at the mirrored image.
helm upgrade --install device-management ./device-management-0.1.0.tgz \
  --set image.registry=internal-registry.example.invalid \
  --set image.repository=device-management \
  --set image.tag=0.8.1 \
  --set imagePullSecrets[0].name=regcred \
  -f my-prod-values.yaml --namespace device-management --create-namespace
```

If the target registry requires auth, create the `imagePullSecrets` Secret
first (`kubectl create secret docker-registry regcred ...`) — the only other
imperative step besides the Secret from "Secret provisioning" above.
`worker.waitForPostgres.image` and `postgres.internal.image` are separate
image references (`postgres:16-alpine` by default) that also need mirroring
if you rely on them; override both the same way as `image.*`.

## Known quirks

- **No env-var renaming.** This chart intentionally maps every env var
  as-is from the reference manifests (`DM_PORT`, `PUBLIC_BASE_URL`,
  `KEYCLOAK_*`, etc.), including cases where a variable's name doesn't
  carry a `DM_` prefix despite being application config (e.g. the
  content-template tokens). Renaming these is tracked separately and is out
  of scope for this chart.
- **`relay-assistant` is not deployed by this chart.** The reference
  manifests include an nginx reverse-proxy in front of Keycloak/other
  upstreams (`relay-assistant`), consumed only by environment-specific
  Gateway/Route objects, not by the base Ingress. It's out of scope for this
  first version of the chart; `config.relay.assistantUrl` and the
  content-template `relayAssistantBaseUrl` token exist so you can point at
  one deployed alongside this release if you need it.
- **Observability (Tempo/Grafana) is not included.** The reference
  manifests ship an optional observability component; this chart only
  covers the application itself. Point `config.telemetry.upstreamEndpoint`
  and `config.telemetry.grafanaUrl` at your own collector/dashboard.
- **`telemetryRelay` runs in `api` mode, not a dedicated mode.** Despite the
  name, `DM_RUNTIME_MODE` has no `telemetry` value — this role reuses the
  default `api` mode with a trimmed-down ConfigMap override
  (`DM_CONFIG_ENABLED=false`, no enroll storage) purely to isolate its
  blast radius from the main `api` Deployment.
- **The migration Job uses `DATABASE_ADMIN_URL`, unlike the old reference
  manifest.** The plain-manifest version only wired `DATABASE_URL` into that
  Job because, in that older flow, a separate imperative script applied
  `schema.sql` with admin credentials before the Alembic Job ran. That
  script no longer exists; migration `001` now embeds `schema.sql` itself
  and needs the privileged DSN directly — see "First install" above.

## Values reference

Every key is commented in `values.yaml`; this is a summary of the top-level
sections.

| Section | Purpose |
|---|---|
| `image` / `imagePullSecrets` | Container image reference and pull credentials. |
| `serviceAccount` | ServiceAccount creation/reuse. |
| `podSecurityContext` / `containerSecurityContext` | Pod/container hardening defaults (non-root uid 10001, seccomp RuntimeDefault, read-only rootfs). Set `podSecurityContext.runAsUser: null` on OpenShift so the cluster SCC assigns the uid. |
| `api` / `admin` / `worker` / `telemetryRelay` | Per-component replicas, resources, probes, autoscaling, extra env/volumes. |
| `migration` | Alembic migration hook Job settings. |
| `ingress` | Optional single Ingress for `api`/`admin`/`telemetryRelay`. Disabled by default. |
| `config` | Non-secret `DM_*`/`KEYCLOAK_*`/`POSTGRES_*`/`AWS_*` application settings, rendered into one ConfigMap. |
| `contentTemplate` | Browser-extension/plugin substitution tokens (`PUBLIC_BASE_URL`, `LLM_BASE_URL`, `API_BASE`, ...) — content, not server config, but rides on the same ConfigMap. |
| `secrets` | Sensitive values. `existingSecret` to reference a Secret managed outside Helm (recommended in production); `create`/`values` for a chart-managed Secret otherwise. |
| `postgres.internal` | Optional dev-only Postgres Deployment. Keep disabled in production. |

## Validation

```bash
helm lint deploy/helm/device-management
helm template deploy/helm/device-management
helm template deploy/helm/device-management -f deploy/helm/examples/values-prod-example.yaml
```
