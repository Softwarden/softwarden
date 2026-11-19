"""The Helm chart's structure (deploy/helm/softwarden). `helm`
isn't on the build machine, so this checks what can be checked without rendering: the chart and values files parse,
every `.Values` path a template uses exists, every `include` names a defined template, blocks balance, the example
values only use real keys, the workloads start the way compose.yaml does, and every variable they set is in
docs/ENV.md. CI runs `helm lint` and `helm template` where helm is installed."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
CHART = REPO / "deploy" / "helm" / "softwarden"
TEMPLATES = CHART / "templates"


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def template_text():
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(TEMPLATES.iterdir()) if p.is_file()}


def has_path(values, dotted):
    node = values
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


FREE_FORM = {"annotations", "podAnnotations", "resources", "nodeSelector", "affinity"}


def unknown_keys(example, values, prefix=""):
    """Keys in an example values file that values.yaml doesn't have. Below a free-form map (annotations, resources,
    nodeSelector, or any map that is empty in values.yaml) any key is allowed."""
    for key, value in example.items():
        if key not in values:
            yield f"{prefix}{key}"
        elif isinstance(value, dict) and isinstance(values[key], dict) and values[key] and key not in FREE_FORM:
            yield from unknown_keys(value, values[key], f"{prefix}{key}.")


def test_chart_metadata():
    chart = load(CHART / "Chart.yaml")
    assert chart["apiVersion"] == "v2" and chart["name"] == "softwarden" and chart["type"] == "application"
    assert chart["appVersion"] == "1.0.0"
    assert re.fullmatch(r"\d+\.\d+\.\d+", chart["version"])
    for name in ("values.yaml", "README.md", "templates/_helpers.tpl", "templates/NOTES.txt"):
        assert (CHART / name).is_file(), name


def test_every_values_path_a_template_uses_exists():
    values = load(CHART / "values.yaml")
    missing = set()
    for name, text in template_text().items():
        for dotted in re.findall(r"\.Values\.([A-Za-z0-9_.]+)", text):
            if not has_path(values, dotted.rstrip(".")):
                missing.add(f"{name}: {dotted}")
    assert not missing, sorted(missing)


def test_includes_name_defined_templates_and_blocks_balance():
    texts = template_text()
    defined = set(re.findall(r'\{\{-?\s*define\s+"([^"]+)"', "".join(texts.values())))
    for name, text in texts.items():
        for used in re.findall(r'include\s+"([^"]+)"', text):
            assert used in defined, (name, used)
        actions = re.findall(r"\{\{-?\s*(if|with|range|define|end)\b", text)
        depth = 0
        for action in actions:
            depth += -1 if action == "end" else 1
            assert depth >= 0, name
        assert depth == 0, name


@pytest.mark.parametrize(
    "example", [*sorted((CHART / "examples").glob("*.yaml")), CHART / "values-cloud.yaml"], ids=lambda p: p.name
)
def test_example_values_use_only_real_keys(example):
    assert not list(unknown_keys(load(example), load(CHART / "values.yaml")))


def test_defaults_point_at_an_external_postgres_and_kept_secrets():
    values = load(CHART / "values.yaml")
    db = values["database"]
    assert {"host", "port", "name", "user", "existingSecret", "existingSecretPasswordKey", "sslmode", "jit"} <= set(db)
    assert db["password"] == "" and db["existingSecret"]  # the password comes from a Secret by default
    assert values["secrets"]["existingSecret"] == ""
    assert "postgres" not in " ".join(template_text())  # no bundled database
    secrets = template_text()["secrets.yaml"]
    for key in ("django_secret_key", "credential_keys", "backup_key", "ops_token"):
        assert f"{key}:" in secrets
    # Generated once, before the migration job, and never deleted by Helm; a reinstall re-emits what exists (lookup)
    # instead of generating new keys or failing on a Secret that is already there.
    assert secrets.count("helm.sh/hook: pre-install\n") == 2 and secrets.count("helm.sh/resource-policy: keep") == 2
    assert secrets.count('(lookup "v1" "Secret" .Release.Namespace $name)') == 2
    assert secrets.count("helm.sh/hook-delete-policy: before-hook-creation") == 2
    for key in ("django_secret_key", "credential_keys", "backup_key", "ops_token"):
        assert f"{key}: {{{{ $data.{key} | default (" in secrets, key


def test_containers_run_on_a_read_only_root_with_a_writable_tmp():
    values = load(CHART / "values.yaml")
    assert values["containerSecurityContext"]["readOnlyRootFilesystem"] is True
    texts = template_text()
    helpers = texts["_helpers.tpl"]
    assert "- name: tmp\n  emptyDir: { sizeLimit: {{ .Values.tmpSizeLimit }} }" in helpers
    # The init container mounts /tmp itself; app, worker and the migration job get it through commonMounts.
    init = helpers[helpers.index('define "softwarden.secretsInit"') :].split("{{- end -}}")[0]
    assert "{ name: tmp, mountPath: /tmp }" in init and ".Values.secretsInit.resources" in init
    for name in ("app.yaml", "worker.yaml", "migrate-job.yaml"):
        assert 'include "softwarden.commonMounts"' in texts[name], name
        assert 'include "softwarden.secretVolumes"' in texts[name], name
        assert "toYaml .Values.containerSecurityContext" in texts[name], name
    assert "{ name: tmp, mountPath: /tmp }" in texts["mcp.yaml"]
    # Extra volumes (a PostgreSQL CA bundle) reach every app-image pod.
    assert ".Values.extraVolumes" in helpers and ".Values.extraVolumeMounts" in helpers


def test_the_mcp_server_takes_the_client_address_from_the_trusted_proxies():
    """Behind the ingress every request reaches the MCP pod from the ingress controller; without the trusted
    proxies its per-address limit on refused tokens would be one limit for the whole install."""
    mcp = template_text()["mcp.yaml"]
    assert "- name: MCP_FORWARDED_ALLOW_IPS\n              value: {{ .Values.trustedProxies | quote }}" in mcp
    values = load(CHART / "values.yaml")
    assert values["trustedProxies"] and "*" not in values["trustedProxies"]  # a list: the rightmost untrusted counts


def test_ingress_allows_uploads_and_disabled_backups_read_as_not_configured():
    values = load(CHART / "values.yaml")
    assert values["ingress"]["annotations"]["nginx.ingress.kubernetes.io/proxy-body-size"] == "26m"
    # Slow request bodies are read by the ingress, never by a gunicorn thread.
    for name in ("values.yaml", "values-cloud.yaml", "examples/external-postgres.yaml"):
        annotations = load(CHART / name)["ingress"]["annotations"]
        assert annotations["nginx.ingress.kubernetes.io/proxy-request-buffering"] == "on", name
    # Threads per gunicorn worker reach the app (each keeps a database connection: docs/CLOUD.md).
    assert values["app"]["gunicornThreads"] == 8
    assert "- name: SOFTWARDEN_THREADS" in template_text()["app.yaml"]
    helpers = template_text()["_helpers.tpl"]
    env = helpers[helpers.index('define "softwarden.env"') :].split("{{- end -}}")[0]
    # In the shared env (app and worker alike), so the app's backup status agrees with the worker.
    assert "if not .Values.persistence.backups.enabled" in env and "name: SOFTWARDEN_BACKUP_DIR" in env


def test_workloads_start_as_compose_does():
    texts = template_text()
    compose = load(REPO / "compose.yaml")["services"]
    app_command = " ".join(compose["app"]["command"])
    assert app_command.endswith("python manage.py bootstrap_admin --prepare && exec gunicorn softwarden.wsgi")
    assert '"python manage.py bootstrap_admin --prepare && exec gunicorn softwarden.wsgi"' in texts["app.yaml"]
    assert "migrate" not in texts["app.yaml"] and "migrate" not in texts["worker.yaml"]
    assert compose["worker"]["command"] == ["python", "manage.py", "run_worker"]
    assert '["python", "manage.py", "run_worker"]' in texts["worker.yaml"]
    # The worker's liveness reads its own pod's heartbeat (several replicas share the database one).
    assert '["python", "manage.py", "run_worker", "--check", "--local"]' in texts["worker.yaml"]
    job = texts["migrate-job.yaml"]
    assert "helm.sh/hook: pre-install,pre-upgrade" in job and '["python", "manage.py", "migrate", "--noinput"]' in job
    # Probes: liveness without the database, readiness without the worker (the image's healthcheck), Host accepted.
    assert "path: /healthz," in texts["app.yaml"] and "path: /readyz/web," in texts["app.yaml"]
    assert "name: Host, value: localhost" in texts["app.yaml"]
    assert "localhost,127.0.0.1" in texts["_helpers.tpl"]
    # The image's user, and the secrets copied private to it.
    values = load(CHART / "values.yaml")
    assert values["podSecurityContext"]["runAsUser"] == 10001 and values["podSecurityContext"]["fsGroup"] == 10001
    assert "umask 077" in texts["_helpers.tpl"] and "mountPath: /data/secrets, readOnly: true" in texts["worker.yaml"]


def test_every_variable_the_chart_sets_is_documented():
    env_md = (REPO / "docs" / "ENV.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"^\|\s*`([A-Z][A-Z0-9_]*)`\s*\|", env_md, re.MULTILINE))
    libpq = {"PGSSLMODE"}  # read by libpq itself, documented in the chart's README
    names = set()
    for text in template_text().values():
        names |= set(re.findall(r"-\s*name:\s*([A-Z][A-Z0-9_]+)\s*$", text, re.MULTILINE))
    assert names, "no environment variables found"
    assert not names - documented - libpq, sorted(names - documented - libpq)


# ---- Cloud deployment (docs/CLOUD.md) ---------------------------------------------------------------------------------


def test_cloud_values_are_hosted_shaped():
    """values-cloud.yaml: cloud mode, managed PostgreSQL over TLS, two or more of each workload with disruption budgets,
    every secret by reference, shared (RWX) storage; no literal secret anywhere."""
    cloud = load(CHART / "values-cloud.yaml")
    assert cloud["deployment"] == "cloud"
    assert cloud["database"]["sslmode"] in ("require", "verify-ca", "verify-full")
    assert cloud["database"]["existingSecret"] and cloud["database"]["password"] == ""
    assert cloud["secrets"]["existingSecret"] and cloud["smtp"]["existingSecret"] and cloud["smtp"]["host"]
    assert cloud["app"]["replicas"] >= 2 and cloud["worker"]["replicas"] >= 2
    assert cloud["podDisruptionBudget"]["enabled"] is True
    assert cloud["app"]["autoscaling"]["enabled"] is False  # optional, off by default
    assert cloud["persistence"]["media"]["accessMode"] == "ReadWriteMany"
    assert cloud["persistence"]["backups"]["accessMode"] == "ReadWriteMany"
    assert cloud["ingress"]["tls"]["enabled"] is True and cloud["ingress"]["tls"]["secretName"]
    assert "password:" not in re.sub(r'password: ""', "", (CHART / "values-cloud.yaml").read_text(encoding="utf-8"))
    values = load(CHART / "values.yaml")
    assert values["deployment"] == "selfhosted" and values["worker"]["replicas"] == 1
    assert values["podDisruptionBudget"]["enabled"] is False and values["app"]["autoscaling"]["enabled"] is False


def test_the_chart_refuses_what_a_hosted_install_mustnt_run_with():
    helpers = template_text()["_helpers.tpl"]
    validate = helpers[helpers.index('define "softwarden.validate"') :].split("{{- end -}}")[0]
    for needle in (
        "secrets.existingSecret",
        "database.existingSecret",
        '(list "require" "verify-ca" "verify-full")',
        "smtp.existingSecret",
        "ReadWriteMany",
        "and $v.smtp.user (not $v.smtp.existingSecret)",
    ):
        assert needle in validate, needle
    assert '{{- include "softwarden.validate" . }}' in template_text()["app.yaml"]


def test_workers_scale_apart_and_only_the_job_migrates():
    texts = template_text()
    assert "replicas: {{ .Values.worker.replicas }}" in texts["worker.yaml"]
    assert "if not .Values.app.autoscaling.enabled" in texts["app.yaml"]
    assert "kind: HorizontalPodAutoscaler" in texts["hpa.yaml"] and "kind: PodDisruptionBudget" in texts["pdb.yaml"]
    job = texts["migrate-job.yaml"]
    assert "parallelism: 1" in job and "completions: 1" in job


def test_spread_constraints_get_the_release_selector():
    """An entry without a labelSelector gets this release's own selector labels (instance included), so values files
    don't hard-code a release name."""
    texts = template_text()
    helpers = texts["_helpers.tpl"]
    spread = helpers[helpers.index('define "softwarden.spread"') :].split("{{- end -}}")[0]
    assert 'include "softwarden.selector"' in spread and 'set $c "labelSelector"' in spread
    assert '"component" "app" "constraints"' in texts["app.yaml"]
    assert '"component" "worker" "constraints"' in texts["worker.yaml"]
    cloud = load(CHART / "values-cloud.yaml")
    for workload in ("app", "worker"):
        for entry in cloud[workload]["topologySpreadConstraints"]:
            assert "labelSelector" not in entry


def test_the_app_sends_hsts_with_ingress_tls_and_pods_mount_no_service_account_token():
    """No Caddy under Helm, so the app sends its own headers (softwarden/security_headers.py) and HSTS while the
    chart's ingress has TLS; no pod gets a Kubernetes API token it never uses."""
    values = load(CHART / "values.yaml")
    assert values["ingress"]["hstsSeconds"] == 31536000 and values["ingress"]["tls"]["enabled"] is True
    texts = template_text()
    helpers = texts["_helpers.tpl"]
    env = helpers[helpers.index('define "softwarden.env"') :].split("{{- end -}}")[0]
    condition = "if and .Values.ingress.enabled .Values.ingress.tls.enabled .Values.ingress.hstsSeconds"
    assert condition in env and "- name: SOFTWARDEN_HSTS_SECONDS" in env
    for name in ("app.yaml", "worker.yaml", "migrate-job.yaml", "mcp.yaml"):
        assert "automountServiceAccountToken: false" in texts[name], name
    # The chart README no longer tells operators to add a CSP on the ingress.
    assert "Don't add a CSP on the ingress" in (CHART / "README.md").read_text(encoding="utf-8")


def test_an_ingress_only_network_policy_is_on_by_default_and_cloud_needs_its_own_trusted_proxies():
    """App and MCP pods admit only the ingress namespace (and the app, this release's MCP pods); cloud mode
    refuses the default trustedProxies, which CI's refusal step renders."""
    values = load(CHART / "values.yaml")
    assert values["networkPolicy"]["enabled"] is True and values["networkPolicy"]["ingressNamespaceSelector"]
    policy = template_text()["networkpolicy.yaml"]
    assert "policyTypes: [Ingress]" in policy and "Egress" not in policy.replace("Egress is untouched", "")
    assert "if .Values.networkPolicy.enabled" in policy
    helpers = template_text()["_helpers.tpl"]
    assert values["trustedProxies"] in helpers and "set trustedProxies" in helpers
    assert load(CHART / "values-cloud.yaml")["trustedProxies"] != values["trustedProxies"]


def test_ci_refusal_cases_escape_commas_in_trusted_proxies():
    r"""`helm --set` splits on commas, so a list value must be written `\,`; unescaped, helm fails parsing and the
    CI step reports a wrong refusal instead of testing the guard."""
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    cases = [line for line in ci.splitlines() if 'refused "set trustedProxies"' in line]
    assert len(cases) >= 5
    for line in cases:
        value = line.split("trustedProxies", 2)[2]
        assert "," not in value.replace(r"\,", ""), line
    assert any("*" in line for line in cases) and any("0.0.0.0/0" in line for line in cases)
    assert 'refused "networkPolicy.extraIngress"' in ci


@pytest.mark.skipif(shutil.which("helm") is None, reason="helm isn't installed")
def test_helm_renders_the_network_policy_and_refuses_unsafe_cloud_proxies():
    def template(*args):
        return subprocess.run(  # noqa: S603
            [shutil.which("helm") or "helm", "template", "s", str(CHART), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    cloud = ["-f", str(CHART / "values-cloud.yaml")]
    default = template("--set", "mcp.enabled=true")
    assert default.returncode == 0 and default.stdout.count("kind: NetworkPolicy") == 2
    assert template(*cloud).returncode == 0
    for value in (
        r"10.0.0.0/8\,172.16.0.0/12\,192.168.0.0/16",
        r"192.168.0.0/16\, 10.0.0.0/8 \,172.16.0.0/12",
        "*",
        "0.0.0.0/0",
        "",
    ):
        result = template(*cloud, "--set", f"trustedProxies={value}")
        assert result.returncode != 0 and "set trustedProxies" in result.stderr, value
    lb = template("--set", "service.type=LoadBalancer")
    assert lb.returncode != 0 and "networkPolicy.extraIngress" in lb.stderr
    peers = template(
        "--set", "service.type=LoadBalancer", "--set", "networkPolicy.extraIngress[0].ipBlock.cidr=10.1.0.0/16"
    )
    assert peers.returncode == 0 and "cidr: 10.1.0.0/16" in peers.stdout
