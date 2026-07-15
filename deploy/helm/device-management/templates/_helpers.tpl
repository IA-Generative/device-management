{{/*
Chart name, truncated/sanitized for use in resource names.
*/}}
{{- define "device-management.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name. Honors fullnameOverride/nameOverride.
*/}}
{{- define "device-management.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
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
Component-scoped name, e.g. "release-device-management-admin".
Usage: {{ include "device-management.componentName" (dict "root" . "component" "admin") }}
*/}}
{{- define "device-management.componentName" -}}
{{- printf "%s-%s" (include "device-management.fullname" .root) .component -}}
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
{{- printf "%s-config" (include "device-management.fullname" .) -}}
{{- end -}}

{{/*
Name of the Secret used at runtime: existingSecret if set, else the chart-managed one.
*/}}
{{- define "device-management.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "device-management.fullname" .) -}}
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
DATABASE_URL/DATABASE_ADMIN_URL secretKeyRef pair, shared by api/admin/worker/telemetryRelay.
Usage: {{ include "device-management.databaseEnv" . }}
*/}}
{{- define "device-management.databaseEnv" -}}
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

{{/*
Maps an ingress path's `service` key (api|admin|telemetryRelay, matching the
values.yaml top-level component keys) to the kebab-case component suffix used
in resource names (api|admin|telemetry-relay).
Usage: {{ include "device-management.serviceComponentSuffix" (dict "service" "telemetryRelay") }}
*/}}
{{- define "device-management.serviceComponentSuffix" -}}
{{- if eq .service "telemetryRelay" -}}
telemetry-relay
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
{{- else -}}
{{- fail (printf "ingress: unknown service %q (expected api|admin|telemetryRelay)" .service) -}}
{{- end -}}
{{- end -}}

{{/*
Hostname of the internal dev postgres Service, used as the wait-for-postgres
default target when postgres.internal.enabled=true. Empty otherwise — an
external database has no such default (worker.waitForPostgres.host must be
set explicitly).
*/}}
{{- define "device-management.postgresHost" -}}
{{- if .Values.postgres.internal.enabled -}}
{{- include "device-management.componentName" (dict "root" . "component" "postgres") -}}
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
