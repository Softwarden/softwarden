{{/* Names and labels. */}}
{{- define "softwarden.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "softwarden.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "softwarden.selector" -}}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "softwarden.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}

{{/* The Secret holding django_secret_key, credential_keys, backup_key (and ops_token): the operator's or the chart's. */}}
{{- define "softwarden.secretName" -}}
{{- .Values.secrets.existingSecret | default (printf "%s-secrets" (include "softwarden.fullname" .)) -}}
{{- end -}}

{{- define "softwarden.mediaClaim" -}}
{{- .Values.persistence.media.existingClaim | default (printf "%s-media" (include "softwarden.fullname" .)) -}}
{{- end -}}

{{- define "softwarden.backupsClaim" -}}
{{- .Values.persistence.backups.existingClaim | default (printf "%s-backups" (include "softwarden.fullname" .)) -}}
{{- end -}}

{{/* Environment shared by app, worker and the migration job (docs/ENV.md). */}}
{{- define "softwarden.env" -}}
- name: SOFTWARDEN_DEPLOYMENT
  value: {{ .Values.deployment | quote }}
- name: SOFTWARDEN_SECRETS_DIR
  value: /data/secrets
- name: SOFTWARDEN_ALLOWED_HOSTS
  value: {{ printf "%s,localhost,127.0.0.1" .Values.publicHostname | quote }}
- name: SOFTWARDEN_CSRF_TRUSTED_ORIGINS
  value: {{ printf "https://%s" .Values.publicHostname | quote }}
- name: SOFTWARDEN_BASE_URL
  value: {{ printf "https://%s" .Values.publicHostname | quote }}
- name: SOFTWARDEN_SECURE_COOKIES
  value: "1"
{{- if and .Values.ingress.enabled .Values.ingress.tls.enabled .Values.ingress.hstsSeconds }}
{{- /* The app sends HSTS itself (on HTTPS requests only); TLS terminates at this chart's ingress. int64:
YAML reads 31536000 as a float, which would render as 3.1536e+07. */}}
- name: SOFTWARDEN_HSTS_SECONDS
  value: {{ .Values.ingress.hstsSeconds | int64 | quote }}
{{- end }}
- name: SOFTWARDEN_TRUSTED_PROXIES
  value: {{ .Values.trustedProxies | quote }}
- name: POSTGRES_HOST
  value: {{ required "database.host is required" .Values.database.host | quote }}
- name: POSTGRES_PORT
  value: {{ .Values.database.port | quote }}
- name: POSTGRES_DB
  value: {{ .Values.database.name | quote }}
- name: POSTGRES_USER
  value: {{ .Values.database.user | quote }}
- name: POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      {{- if .Values.database.existingSecret }}
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.existingSecretPasswordKey }}
      {{- else }}
      name: {{ include "softwarden.fullname" . }}-database
      key: password
      {{- end }}
- name: SOFTWARDEN_DB_JIT
  value: {{ .Values.database.jit | quote }}
{{- if not .Values.persistence.backups.enabled }}
{{- /* The image sets /data/backups; empty means "not configured", for the worker and the app's status alike. */}}
- name: SOFTWARDEN_BACKUP_DIR
  value: ""
{{- end }}
{{- with .Values.database.sslmode }}
- name: POSTGRES_SSLMODE
  value: {{ . | quote }}
{{- end }}
{{- with .Values.database.sslrootcert }}
- name: POSTGRES_SSLROOTCERT
  value: {{ . | quote }}
{{- end }}
{{- with .Values.database.connMaxAge }}
- name: SOFTWARDEN_DB_CONN_MAX_AGE
  value: {{ . | quote }}
{{- end }}
{{- if .Values.secrets.existingSecret }}
{{- /* Keys the operator created and keeps elsewhere: read as configured files, not as generated ones (doctor's cloud
check, and no "copy the generated backup key off the host" warning). */}}
- name: SOFTWARDEN_CREDENTIAL_KEYS_FILE
  value: /data/secrets/credential_keys
- name: SOFTWARDEN_BACKUP_KEY_FILE
  value: /data/secrets/backup_key
{{- end }}
{{- if .Values.smtp.host }}
- name: SOFTWARDEN_SMTP_HOST
  value: {{ .Values.smtp.host | quote }}
- name: SOFTWARDEN_SMTP_PORT
  value: {{ .Values.smtp.port | quote }}
- name: SOFTWARDEN_SMTP_USER
  value: {{ .Values.smtp.user | quote }}
- name: SOFTWARDEN_SMTP_TLS
  value: {{ ternary "1" "0" .Values.smtp.tls | quote }}
- name: SOFTWARDEN_EMAIL_FROM
  value: {{ .Values.smtp.from | quote }}
{{- if .Values.smtp.existingSecret }}
- name: SOFTWARDEN_SMTP_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Values.smtp.existingSecret }}
      key: {{ .Values.smtp.existingSecretPasswordKey }}
{{- end }}
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
topologySpreadConstraints for a workload, from (dict "root" . "component" "app"|"worker" "constraints" list): an entry
without a labelSelector gets this release's own selector labels for the component (name, instance, component), so the
spread counts only this release's pods of that kind.
*/}}
{{- define "softwarden.spread" -}}
{{- $selector := include "softwarden.selector" (dict "root" .root "component" .component) | fromYaml -}}
{{- range .constraints }}
{{- $c := deepCopy . }}
{{- if not $c.labelSelector }}
{{- $_ := set $c "labelSelector" (dict "matchLabels" $selector) }}
{{- end }}
- {{ toYaml $c | nindent 2 | trim }}
{{- end }}
{{- end -}}

{{/*
Refuses values that can't work, at render time (helm lint, template, install). Included once, by app.yaml.
*/}}
{{- define "softwarden.validate" -}}
{{- $v := .Values -}}
{{- if not (has $v.deployment (list "selfhosted" "cloud")) }}
{{- fail (printf "deployment must be selfhosted or cloud, not %q" $v.deployment) }}
{{- end }}
{{- $several := or (gt (int $v.app.replicas) 1) $v.app.autoscaling.enabled (gt (int $v.worker.replicas) 1) }}
{{- if and $several (ne $v.persistence.media.accessMode "ReadWriteMany") (not $v.persistence.media.existingClaim) }}
{{- fail "More than one app or worker pod shares the attachment files: set persistence.media.accessMode ReadWriteMany (an RWX storage class), or bring a claim in persistence.media.existingClaim" }}
{{- end }}
{{- if and (gt (int $v.worker.replicas) 1) $v.persistence.backups.enabled (ne $v.persistence.backups.accessMode "ReadWriteMany") (not $v.persistence.backups.existingClaim) }}
{{- fail "More than one worker mounts the backup volume: set persistence.backups.accessMode ReadWriteMany, bring a claim in persistence.backups.existingClaim, or turn persistence.backups.enabled off" }}
{{- end }}
{{- if and $v.networkPolicy.enabled (ne $v.service.type "ClusterIP") (not $v.networkPolicy.extraIngress) }}
{{- fail (printf "service.type is %s and networkPolicy is on: traffic from a load balancer or node isn't from a pod, so the policy would drop it. Set networkPolicy.extraIngress (NetworkPolicy peers, e.g. [{ipBlock: {cidr: <load balancer or node range>}}]), or networkPolicy.enabled=false" $v.service.type) }}
{{- end }}
{{- if eq $v.deployment "cloud" }}
{{- if not $v.secrets.existingSecret }}
{{- fail "deployment cloud: set secrets.existingSecret; a hosted installation's keys are created and kept by the operator, never generated by the chart (docs/CLOUD.md)" }}
{{- end }}
{{- if not $v.database.existingSecret }}
{{- fail "deployment cloud: set database.existingSecret; the database password never goes in values" }}
{{- end }}
{{- if not (has $v.database.sslmode (list "require" "verify-ca" "verify-full")) }}
{{- fail "deployment cloud: set database.sslmode to require, verify-ca or verify-full (a managed PostgreSQL over TLS)" }}
{{- end }}
{{- $proxies := splitList "," (nospace (toString $v.trustedProxies)) | compact | sortAlpha }}
{{- $stock := splitList "," "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16" | sortAlpha }}
{{- if or (not $proxies) (eq (toString $proxies) (toString $stock)) (has "*" $proxies) (has "0.0.0.0/0" $proxies) (has "::/0" $proxies) }}
{{- fail "deployment cloud: set trustedProxies to your ingress controller's pod CIDR; empty, the default (every private range), * and 0.0.0.0/0 let any pod in the cluster claim a client address" }}
{{- end }}
{{- if not $v.smtp.host }}
{{- fail "deployment cloud: set smtp.host; signup confirms every address by email" }}
{{- end }}
{{- if and $v.smtp.user (not $v.smtp.existingSecret) }}
{{- fail "deployment cloud: smtp.user is set, so set smtp.existingSecret for its password; it never goes in values" }}
{{- end }}
{{- end }}
{{- end -}}

{{/*
The generated-secret files, as the Compose init service leaves them: 0600 files owned by the app user in
/data/secrets. A Secret volume's files belong to root, and the backup refuses a key readable by group or others, so an
init container (the app's own image, as uid 10001) copies them from the Secret into a private in-memory volume.
*/}}
{{- define "softwarden.secretsInit" -}}
- name: secrets
  image: {{ include "softwarden.image" . }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  command: ["sh", "-c", "umask 077 && for f in /secret-src/*; do cat \"$f\" > \"/data/secrets/${f##*/}\"; done"]
  resources:
    {{- toYaml .Values.secretsInit.resources | nindent 4 }}
  securityContext:
    {{- toYaml .Values.containerSecurityContext | nindent 4 }}
  volumeMounts:
    - { name: secret-src, mountPath: /secret-src, readOnly: true }
    - { name: secrets, mountPath: /data/secrets }
    - { name: tmp, mountPath: /tmp }
{{- end -}}

{{/* Mounts every app-image container has: the writable /tmp (the root filesystem is read-only) and extraVolumeMounts. */}}
{{- define "softwarden.commonMounts" -}}
- { name: tmp, mountPath: /tmp }
{{- with .Values.extraVolumeMounts }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "softwarden.secretVolumes" -}}
- name: secret-src
  secret:
    secretName: {{ include "softwarden.secretName" . }}
    defaultMode: 0440
- name: secrets
  emptyDir: { medium: Memory, sizeLimit: 1Mi }
- name: tmp
  emptyDir: { sizeLimit: {{ .Values.tmpSizeLimit }} }
{{- with .Values.extraVolumes }}
{{ toYaml . }}
{{- end }}
{{- end -}}
