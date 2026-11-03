{{/*
Chart name, truncated/sanitized for use in resource names.
*/}}
{{- define "device-management.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name. Honors fullnameOverride/nameOverride.

Truncated to 45 (not 63): every derived resource name appends a suffix on
top of this — the longest chain is componentName's "-telemetry-relay" (16
chars), and postgres.cnpg additionally lets the CNPG *operator* itself
append "-superuser"/"-rw"/"-app" (up to 10 more chars) to the Cluster name
it derives from this, outside Helm's own rendering where nothing here can
catch an overflow at template time. 45 + 16 = 61 (component chain) and
45 + "-db-superuser" (13) = 58 (CNPG chain) both stay under the Kubernetes
63-character label-value/DNS-label limit. Release names can legitimately be
up to 53 chars on their own (Helm's own cap), so without this, a release
name that doesn't happen to already contain "device-management" silently
produces resource names that fail at apply time (see the
`spec.template.labels: ... must be no more than 63 characters` failure on
the migrate Job for a real example).
*/}}
{{- define "device-management.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 45 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 45 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 45 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "device-management.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels, merged with .Values.commonLabels.
*/}}
{{- define "device-management.labels" -}}
helm.sh/chart: {{ include "device-management.chart" . }}
{{ include "device-management.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Selector labels shared by every component (does not vary per-component so
Services/HPAs/Deployments agree) plus a component-specific "app.kubernetes.io/component".
Usage: {{ include "device-management.selectorLabels" . }}
*/}}
{{- define "device-management.selectorLabels" -}}
app.kubernetes.io/name: {{ include "device-management.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Component-scoped name, e.g. "release-device-management-admin". Truncated to
63 (the Kubernetes label-value/DNS-label limit) as a defense-in-depth
safety net on top of fullname's own 45-char budget — see the comment on
"device-management.fullname" for why 45 was chosen.
Usage: {{ include "device-management.componentName" (dict "root" . "component" "admin") }}
*/}}
{{- define "device-management.componentName" -}}
{{- printf "%s-%s" (include "device-management.fullname" .root) .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Component-scoped selector labels (adds app.kubernetes.io/component).
Usage: {{ include "device-management.componentSelectorLabels" (dict "root" . "component" "admin") }}
*/}}
{{- define "device-management.componentSelectorLabels" -}}
{{ include "device-management.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "device-management.componentLabels" -}}
{{ include "device-management.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
ServiceAccount name.
*/}}
{{- define "device-management.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "device-management.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Name of the ConfigMap holding non-secret DM_* config.
*/}}
{{- define "device-management.configMapName" -}}
{{- printf "%s-config" (include "device-management.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Name of the Secret used at runtime: existingSecret if set, else the chart-managed one.
*/}}
{{- define "device-management.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "device-management.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Fully qualified image reference, honoring image.registry/repository/tag
(tag falls back to .Chart.AppVersion).
*/}}
{{- define "device-management.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion -}}
{{- if .Values.image.registry -}}
{{- printf "%s/%s:%s" .Values.image.registry .Values.image.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}
{{- end -}}

{{/*
Pod-level securityContext, honoring the OpenShift-compatible override
(podSecurityContext.runAsUser: null omits the field so the cluster SCC can
assign an arbitrary uid).
*/}}
{{- define "device-management.podSecurityContext" -}}
{{- $sc := .Values.podSecurityContext -}}
runAsNonRoot: {{ $sc.runAsNonRoot }}
{{- if not (kindIs "invalid" $sc.runAsUser) }}
runAsUser: {{ $sc.runAsUser }}
{{- end }}
{{- if not (kindIs "invalid" $sc.runAsGroup) }}
runAsGroup: {{ $sc.runAsGroup }}
{{- end }}
{{- if not (kindIs "invalid" $sc.fsGroup) }}
fsGroup: {{ $sc.fsGroup }}
{{- end }}
{{- if $sc.seccompProfileType }}
seccompProfile:
  type: {{ $sc.seccompProfileType }}
{{- end }}
{{- end -}}

{{/*
Container-level securityContext (allowPrivilegeEscalation/readOnlyRootFilesystem/capabilities).
*/}}
{{- define "device-management.containerSecurityContext" -}}
{{ toYaml .Values.containerSecurityContext }}
{{- end -}}

{{/*
Shared "pod identity" env entries wired into every DM_* role: POD_IP/NODE_NAME
(fieldRef), DM_APP_VERSION, and the runtime-config poll/editable pair. These
back the cross-pod runtime config broadcast (see app/runtime_config.py).
Usage: {{ include "device-management.identityEnv" (dict "root" . "pollIntervalSeconds" 3 "editable" true) }}
*/}}
{{- define "device-management.identityEnv" -}}
- name: POD_IP
  valueFrom:
    fieldRef:
      fieldPath: status.podIP
- name: NODE_NAME
  valueFrom:
    fieldRef:
      fieldPath: spec.nodeName
- name: DM_APP_VERSION
  value: {{ .root.Chart.AppVersion | quote }}
- name: DM_CONFIG_POLL_INTERVAL_SECONDS
  value: {{ .pollIntervalSeconds | quote }}
- name: DM_RUNTIME_CONFIG_EDITABLE
  value: {{ .editable | quote }}
- name: DM_CONFIG_SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "device-management.secretName" .root }}
      key: DM_CONFIG_SECRET_KEY
      optional: true
{{- end -}}

{{/*
DATABASE_URL/DATABASE_ADMIN_URL secretKeyRef pair, shared by api/admin/worker/telemetryRelay/migrate.
When postgres.cnpg.enabled, both are sourced from the CNPG operator's
`<cluster>-app` Secret (key `uri`) — CNPG generates and rotates it itself.
The migrations run with the application role on purpose: the tables must be
owned by it, and the extensions are pre-created at cluster bootstrap
(postInitApplicationSQL), so no superuser access is needed.
Usage: {{ include "device-management.databaseEnv" . }}
*/}}
{{- define "device-management.databaseEnv" -}}
{{- if .Values.postgres.cnpg.enabled -}}
{{- $cluster := include "device-management.cnpgClusterName" . -}}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ $cluster }}-app
      key: uri
# Intentionally the same app-role Secret as DATABASE_URL, not genuinely
# admin-privileged: alembic (the migration Job) uses whichever of
# DATABASE_ADMIN_URL/DATABASE_URL is set for its ENTIRE run, not just the
# initial CREATE EXTENSION step — pointing this at CNPG's superuser role
# would make every table it creates owned by that role instead of the
# app's, breaking the app's own runtime access to them. Components that
# separately need genuine superuser credentials (see
# device-management.cnpgSuperuserAdminUrlOverride) must override this
# afterwards in their own env: list, not here.
- name: DATABASE_ADMIN_URL
  valueFrom:
    secretKeyRef:
      name: {{ $cluster }}-app
      key: uri
{{- else -}}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "device-management.secretName" . }}
      key: DATABASE_URL
- name: DATABASE_ADMIN_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "device-management.secretName" . }}
      key: DATABASE_ADMIN_URL
{{- end -}}
{{- end -}}

{{/*
STOPGAP — remove once the upstream device-management fix (making the
FastAPI startup hook's admin-privileged role/database/schema bootstrap
opt-in, off by default) is deployed. Until then, that hook runs
unconditionally on every api/admin/telemetryRelay/llmProxy pod boot (they
all run `uvicorn app.main:app`) and genuinely needs postgres-superuser
credentials to CREATE ROLE/CREATE DATABASE — which DATABASE_ADMIN_URL from
device-management.databaseEnv does NOT provide (deliberately: alembic uses
that same value for its entire migration run, so it stays pointed at the
app role to avoid tables ending up owned by a role the app can't use).
This appends a second DATABASE_ADMIN_URL entry sourced from CNPG's
enableSuperuserAccess-managed Secret; Kubernetes uses the LAST entry with a
given name in a container's env: list, so this overrides the one from
databaseEnv above as long as it's included afterwards.
Only include this on components running the FastAPI app (api/admin/
telemetryRelay/llmProxy) — never on the migration Job or worker.
Usage: {{ include "device-management.cnpgSuperuserAdminUrlOverride" . }}
(placed AFTER device-management.databaseEnv in that component's env: list)
*/}}
{{- define "device-management.cnpgSuperuserAdminUrlOverride" -}}
{{- if and .Values.postgres.cnpg.enabled .Values.postgres.cnpg.temporaryStartupBootstrapNeedsSuperuser -}}
{{- $cluster := include "device-management.cnpgClusterName" . -}}
- name: DATABASE_ADMIN_URL
  valueFrom:
    secretKeyRef:
      name: {{ $cluster }}-superuser
      key: uri
{{- end -}}
{{- end -}}

{{/*
Maps an ingress path's `service` key (api|admin|telemetryRelay|llmProxy,
matching the values.yaml top-level component keys) to the kebab-case
component suffix used in resource names (api|admin|telemetry-relay|llm-proxy).
Usage: {{ include "device-management.serviceComponentSuffix" (dict "service" "telemetryRelay") }}
*/}}
{{- define "device-management.serviceComponentSuffix" -}}
{{- if eq .service "telemetryRelay" -}}
telemetry-relay
{{- else if eq .service "llmProxy" -}}
llm-proxy
{{- else -}}
{{- .service -}}
{{- end -}}
{{- end -}}

{{/*
Returns the Service port number for an ingress path's `service` key.
Usage: {{ include "device-management.ingressServicePort" (dict "root" $ "service" "admin") }}
*/}}
{{- define "device-management.ingressServicePort" -}}
{{- if eq .service "api" -}}
{{- .root.Values.api.service.port -}}
{{- else if eq .service "admin" -}}
{{- .root.Values.admin.service.port -}}
{{- else if eq .service "telemetryRelay" -}}
{{- .root.Values.telemetryRelay.service.port -}}
{{- else if eq .service "llmProxy" -}}
{{- .root.Values.llmProxy.service.port -}}
{{- else -}}
{{- fail (printf "ingress: unknown service %q (expected api|admin|telemetryRelay|llmProxy)" .service) -}}
{{- end -}}
{{- end -}}

{{/*
Name of the CNPG Cluster resource (and the prefix of its operator-managed
`-rw`/`-app`/`-superuser` Services/Secrets).
*/}}
{{- define "device-management.cnpgClusterName" -}}
{{- include "device-management.componentName" (dict "root" . "component" "db") -}}
{{- end -}}

{{/*
Hostname of the cluster-provisioned postgres Service, used as the
wait-for-postgres default target when postgres.internal.enabled=true or
postgres.cnpg.enabled=true. Empty otherwise — an external database has no
such default (waitForPostgres.host must be set explicitly in that case).
*/}}
{{- define "device-management.postgresHost" -}}
{{- if .Values.postgres.internal.enabled -}}
{{- include "device-management.componentName" (dict "root" . "component" "postgres") -}}
{{- else if .Values.postgres.cnpg.enabled -}}
{{- printf "%s-rw" (include "device-management.cnpgClusterName" .) -}}
{{- end -}}
{{- end -}}

{{/*
AWS credentials secretKeyRef triplet (S3 storage).
Usage: {{ include "device-management.awsEnv" . }}
*/}}
{{- define "device-management.awsEnv" -}}
- name: AWS_ACCESS_KEY_ID
  valueFrom:
    secretKeyRef:
      name: {{ include "device-management.secretName" . }}
      key: AWS_ACCESS_KEY_ID
- name: AWS_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "device-management.secretName" . }}
      key: AWS_SECRET_ACCESS_KEY
- name: AWS_SESSION_TOKEN
  valueFrom:
    secretKeyRef:
      name: {{ include "device-management.secretName" . }}
      key: AWS_SESSION_TOKEN
{{- end -}}
