"""Upgrade and release engineering: CI
runs the deployment checks and the upgrade rehearsal from the previous release (ops/ci/restore-previous.sh), release.yml
publishes both images to GHCR from a v* tag with notes from CHANGELOG.md, and the docs name the images the way the
workflow publishes them. Running the rehearsal itself needs Docker and minutes; CI's migrate-from-previous job does."""

import json
import os
import re
import shutil
import subprocess
import tomllib

import pytest
import yaml
from packaging.version import InvalidVersion, Version

from tests.test_compose import REPO, services

CI = REPO / ".github" / "workflows" / "ci.yml"
RELEASE = REPO / ".github" / "workflows" / "release.yml"
SITE = REPO / ".github" / "workflows" / "site.yml"
RESTORE_PREVIOUS = REPO / "ops" / "ci" / "restore-previous.sh"
CHANGELOG_SECTION = REPO / "ops" / "ci" / "changelog-section.sh"
PREFLIGHT_SCRIPT = REPO / "ops" / "ci" / "release-preflight.sh"
OTEL_PROBE = REPO / "ops" / "ci" / "otel-image-probe.sh"
INAPP_BACKUP = REPO / "ops" / "ci" / "inapp-backup-rehearsal.sh"


def workflow(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    # YAML 1.1 reads the `on:` key as True.
    data["on"] = data.pop(True, data.get("on"))
    return data


def steps_text(job):
    return "\n".join(str(step.get("run", "")) + " " + str(step.get("uses", "")) for step in job["steps"])


def test_ci_databases_use_the_tag_compose_pins():
    db_image = services("compose.yaml")["db"]["image"]
    jobs = workflow(CI)["jobs"]
    found = [job["services"]["postgres"]["image"] for job in jobs.values() if "postgres" in job.get("services", {})]
    assert found and all(image == db_image for image in found), (db_image, found)


def test_ci_has_the_deployment_and_audit_jobs():
    jobs = workflow(CI)["jobs"]
    for name in ("api", "mcp", "web", "image", "e2e", "audit", "deploy-config", "compose-first-run"):
        assert name in jobs, name
    audit = steps_text(jobs["audit"])
    assert "uv export --frozen --no-dev" in audit and "uvx pip-audit" in audit
    assert "npm audit --omit=dev --json" in audit and "ops/ci/npm-audit-check.py" in audit  # moderate and above
    deploy = steps_text(jobs["deploy-config"])
    assert "azure/setup-helm" in deploy
    assert "helm lint deploy/helm/softwarden -f deploy/helm/softwarden/examples/external-postgres.yaml" in deploy
    assert "helm template softwarden deploy/helm/softwarden > /dev/null" in deploy
    assert ".services.proxy.image" in deploy and "caddy validate" in deploy  # the tag compose.yaml pins
    first_run = steps_text(jobs["compose-first-run"])
    for check in ("up -d --build --wait", "bootstrap_admin --prepare", "doctor --json", "/readyz"):
        assert check in first_run, check
    assert "down --volumes" in first_run
    e2e = jobs["e2e"]
    assert e2e["strategy"]["matrix"]["shard"] == [1, 2, 3, 4]
    assert "npx playwright test --shard ${{ matrix.shard }}/4" in steps_text(e2e)


def test_migrate_from_previous_runs_the_rehearsal_script_with_full_history():
    job = workflow(CI)["jobs"]["migrate-from-previous"]
    checkout = next(step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0  # the release tags and their history
    assert "bash ops/ci/restore-previous.sh" in steps_text(job)
    assert "placeholder" not in job["name"].lower()


def test_restore_previous_rehearses_from_the_previous_release_tag():
    script = RESTORE_PREVIOUS.read_text(encoding="utf-8")
    # No pinned fallback commit: without a v* tag the rehearsal skips (exit 0) before anything is built or started.
    assert "FALLBACK" not in script
    assert script.index("SKIPPED:") < script.index("trap on_exit EXIT") < script.index("command -v docker")
    # Releases HEAD descends from, not one at HEAD (on the v1.1.0 tag, the previous release is v1.0.0).
    assert "tag --list 'v*' --sort=-v:refname --merged HEAD --no-contains HEAD" in script
    assert "worktree add --detach" in script and "worktree remove --force" in script
    assert 'prev_project="sw-restore-prev-$run_id"' in script and 'cur_project="sw-restore-cur-$run_id"' in script
    assert "python manage.py seed_demo" in script
    assert '"$prev_dir/ops/backup.sh"' in script  # the previous release's own backup script
    assert 'restore.sh" "${untagged[@]}" --rehearse --migrate "$set_file"' in script
    assert 'restore.sh" "${untagged[@]}" --yes "$set_file"' in script
    assert "untagged=(--accept-untagged)" in script  # only for a release before integrity tags
    for check in ("migrate --check", "upgrade_check --manifest", "manage.py doctor"):
        assert check in script, check
    # Teardown is scoped to the run's own projects: compose down for each, then only resources with its label.
    assert "down --volumes --remove-orphans" in script
    assert "label=com.docker.compose.project=$name" in script
    assert "docker volume prune" not in script and "docker system prune" not in script
    assert "set -euo pipefail" in script and "trap on_exit EXIT" in script
    assert "exec </dev/null" in script
    # Before anything starts, both projects (the ops scripts through load_config, and compose itself) must resolve to
    # the run's own names, so a local ops/backup.env can't aim restore.sh --yes at the developer's real stack.
    assert 'resolved_name prev_ops "$prev_dir")" = "$prev_project"' in script
    assert 'resolved_name cur_ops "$repo")" = "$cur_project"' in script
    assert script.index("checking that every command aims") < script.index("starting the previous release")
    assert not re.search(r"(^\s*|[;&|]\s*)read[ \t]", script, re.MULTILINE)  # never prompts


def test_restore_previous_skips_without_a_release_tag_and_rehearses_from_one(tmp_path):
    """Run the script in a throwaway repository with a stub `docker`: with no v* tag it skips (exit 0) and calls
    nothing; once a release is tagged it picks that tag and goes on to the rehearsal (the stub then stops it)."""
    bash, git = shutil.which("bash"), shutil.which("git")
    if bash is None or git is None:
        pytest.skip("bash and git are needed")
    repo = tmp_path / "repo"
    (repo / "ops" / "ci").mkdir(parents=True)
    (repo / "ops" / "ci" / "restore-previous.sh").write_bytes(RESTORE_PREVIOUS.read_bytes())
    (repo / "ops" / "common.sh").write_bytes((REPO / "ops" / "common.sh").read_bytes())
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "docker-calls.txt"
    # Only `docker compose version` succeeds, so a rehearsal that starts stops at its first real docker command.
    (stubs / "docker").write_bytes(
        b'#!/bin/sh\necho "docker $*" >> "$STUB_CALLS"\n[ "$1 $2" = "compose version" ] && exit 0\nexit 1\n'
    )
    (stubs / "openssl").write_bytes(b"#!/bin/sh\nexit 0\n")
    env = {
        **os.environ,
        "STUB_CALLS": calls.as_posix(),
        "TMPDIR": tmp_path.as_posix(),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
    }
    env.pop("GITHUB_ACTIONS", None)

    # The stub folder as bash names it (Git Bash on Windows needs /c/..., since PATH is colon-separated).
    to_unix = f"cygpath -u '{stubs.as_posix()}' 2>/dev/null || printf %s '{stubs.as_posix()}'"

    def sh(command):
        return subprocess.run(  # noqa: S603
            [bash, "-c", f'export PATH="$({to_unix})":"$PATH"; {command}'],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    for command in ("chmod +x ../stubs/docker ../stubs/openssl", "git init -q", "git add -A", "git commit -qm one"):
        assert sh(command).returncode == 0, command

    skipped = sh("bash ops/ci/restore-previous.sh")
    assert skipped.returncode == 0, skipped.stderr
    assert "SKIPPED" in skipped.stdout + skipped.stderr
    assert not calls.exists()  # nothing was built or started

    assert sh("git tag v0.1.0 && git commit -q --allow-empty -m two").returncode == 0
    rehearsed = sh("bash ops/ci/restore-previous.sh")
    out = rehearsed.stdout + rehearsed.stderr
    assert "SKIPPED" not in out
    assert "previous release: v0.1.0" in out, out
    assert calls.exists() and "docker compose version" in calls.read_text()
    assert rehearsed.returncode != 0  # the stub refuses the rehearsal's first real docker command


@pytest.mark.parametrize("path", [RESTORE_PREVIOUS, CHANGELOG_SECTION, PREFLIGHT_SCRIPT, OTEL_PROBE, INAPP_BACKUP])
def test_ci_scripts_parse_and_keep_lf_endings(path):
    assert b"\r\n" not in path.read_bytes()  # bash on the Linux runner reads a CR as part of each command
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash isn't installed here")
    result = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)  # noqa: S603
    assert result.returncode == 0, result.stderr


def uses(job, prefix):
    return [step for step in job.get("steps", []) if str(step.get("uses", "")).startswith(prefix)]


def test_release_checks_everything_before_it_pushes_and_tags_only_at_the_end():
    """Preflight (tag, main, versions, unpublished), the whole CI, an approval, push by digest with attestation,
    a smoke test of the digests, and only then the tags and the GitHub Release."""
    data = workflow(RELEASE)
    assert data["on"] == {"push": {"tags": ["v*"]}}
    assert data["permissions"] == {}  # nothing by default; each job asks for its own
    jobs = data["jobs"]
    assert list(jobs) == ["preflight", "ci", "approve", "images", "smoke", "publish"]
    assert jobs["preflight"]["permissions"] == {"contents": "read", "packages": "read"}
    assert jobs["images"]["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }
    assert jobs["smoke"]["permissions"] == {"contents": "read", "packages": "read"}
    assert jobs["publish"]["permissions"] == {"contents": "write", "packages": "write"}
    for job in jobs.values():
        for step in uses(job, "actions/checkout"):
            assert step["with"]["persist-credentials"] is False

    # The order: nothing is pushed before preflight, CI and the approval; nothing is tagged before both images are
    # attested and smoke-tested.
    assert jobs["ci"]["needs"] == "preflight" and jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert set(jobs["approve"]["needs"]) == {"preflight", "ci"} and jobs["approve"]["environment"] == "release"
    assert set(jobs["images"]["needs"]) == {"preflight", "approve"}
    assert jobs["smoke"]["needs"] == "images"
    assert set(jobs["publish"]["needs"]) == {"preflight", "images", "smoke"}
    assert "workflow_call" in workflow(CI)["on"]

    preflight = steps_text(jobs["preflight"])
    for check in ("release-preflight.sh version", "release-preflight.sh repo", "release-preflight.sh changelog-date"):
        assert check in preflight, check
    assert "release-preflight.sh unpublished" in preflight and "gh release view" in preflight
    assert "ops/ci/changelog-section.sh" in preflight
    assert uses(jobs["preflight"], "actions/checkout")[0]["with"]["fetch-depth"] == 0  # origin/main

    images = {entry["image"]: entry for entry in jobs["images"]["strategy"]["matrix"]["include"]}
    assert set(images) == {"softwarden", "softwarden-mcp"}
    assert images["softwarden"]["context"] == "." and images["softwarden"]["file"] == "Dockerfile"
    assert images["softwarden-mcp"]["file"] == "mcp/Dockerfile"
    assert images["softwarden-mcp"]["build-contexts"] == "cacerts=ca-certs"
    text = RELEASE.read_text(encoding="utf-8")
    assert "ghcr.io/${GITHUB_REPOSITORY_OWNER,,}/${{ matrix.image }}" in text
    (build,) = uses(jobs["images"], "docker/build-push-action")
    assert build["with"]["sbom"] is True and build["with"]["provenance"] == "mode=max"
    assert "push-by-digest=true" in build["with"]["outputs"] and "tags" not in build["with"]
    assert "push" not in build["with"]  # by digest only
    assert build["with"]["no-cache"] is True and "cache-from" not in build["with"] and "cache-to" not in build["with"]
    assert "type=gha" not in text
    (meta,) = uses(jobs["images"], "docker/metadata-action")
    assert "tags" not in meta["with"]  # labels only; the tags come at the end
    steps = [str(step.get("uses", "")) for step in jobs["images"]["steps"]]
    attest = next(i for i, s in enumerate(steps) if s.startswith("actions/attest-build-provenance"))
    assert next(i for i, s in enumerate(steps) if s.startswith("docker/build-push-action")) < attest

    smoke = steps_text(jobs["smoke"])
    assert "up -d --no-build --pull missing --wait" in smoke and "/readyz" in smoke and "down --volumes" in smoke

    publish = steps_text(jobs["publish"])
    assert "release-preflight.sh tags" in publish and "release-preflight.sh unpublished" in publish
    assert "docker buildx imagetools create" in publish
    # X.Y.Z on both images before any moving tag, and the Release last; one publish at a time across tags.
    run = next(step["run"] for step in jobs["publish"]["steps"] if step.get("name") == "Tag the digests")
    assert run.index('--tag "ghcr.io/$owner/$image:$VERSION"') < run.index('"${tags[@]:1}"')
    assert '"${#tags[@]}" -eq 0' in run
    assert jobs["publish"]["concurrency"] == {"group": "release-publish", "cancel-in-progress": False}
    assert 'grep -qi "release not found"' in preflight
    assert "gh release create" in publish and '"--latest=$LATEST"' in publish
    assert "digest" in publish and "GH_REPO: ${{ github.repository }}" in text


PREFLIGHT = PREFLIGHT_SCRIPT


@pytest.fixture
def preflight(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash isn't installed here")

    def run(*args, stdin="", cwd=None, env=None):
        return subprocess.run(  # noqa: S603
            [bash, str(PREFLIGHT), *args],
            capture_output=True,
            text=True,
            input=stdin,
            cwd=cwd or tmp_path,
            env={**os.environ, **(env or {})},
        )

    return run


@pytest.mark.parametrize("tag", ["v1.0.0", "v0.1.0", "v10.20.30", "v1.1.0-rc.1", "v1.1.0-alpha.0", "v2.0.0-x-y.7"])
def test_preflight_accepts_a_version_tag(preflight, tag):
    result = preflight("version", tag)
    assert result.returncode == 0, result.stderr
    version = tag[1:]
    assert result.stdout.splitlines() == [f"version={version}", f"prerelease={str('-' in version).lower()}"]


@pytest.mark.parametrize(
    "tag",
    ["v01.2.3", "v1.02.3", "v1.2.03", "v1.2.3-rc.01", "v1.2.3-", "v1.2.3-rc..1", "v1.2.3-rc.", "1.2.3", "v1.2",
     "v1.2.3+build", "v1.2.3-rc_1", "vx.y.z"],
)  # fmt: skip
def test_preflight_refuses_anything_else(preflight, tag):
    result = preflight("version", tag)
    assert result.returncode == 1 and result.stdout == ""
    assert "isn't a version tag" in result.stderr


CHANGELOG_DATES = """# Changelog

## [Unreleased]

## [1.1.0] - {date}

- notes

## [1.0.0] - 2026-11-08

- old
"""


@pytest.mark.parametrize(
    ("date", "error"),
    [
        ("2026-11-09", None),
        ("2026-11-12", None),  # tomorrow, for a tag pushed just before midnight somewhere
        ("unreleased (set on tag day)", "has no '## [1.1.0] - YYYY-MM-DD' heading"),
        ("9999-99-99", "isn't a calendar date"),
        ("2026-02-30", "isn't a calendar date"),
        ("2026-13-01", "isn't a calendar date"),
        ("2026-11-13", "is in the future"),
        ("9999-12-31", "is in the future"),
    ],
)
def test_preflight_changelog_date_must_be_a_real_day_not_after_tomorrow(preflight, tmp_path, date, error):
    (tmp_path / "CHANGELOG.md").write_bytes(CHANGELOG_DATES.format(date=date).encode())
    result = preflight("changelog-date", "1.1.0", "CHANGELOG.md", env={"RELEASE_TODAY": "2026-11-11"})
    if error is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1 and error in result.stderr, result.stderr


def test_preflight_changelog_date_reads_only_its_own_version(preflight, tmp_path):
    (tmp_path / "CHANGELOG.md").write_bytes(CHANGELOG_DATES.format(date="2026-11-09").encode())
    assert preflight("changelog-date", "1.0.0", env={"RELEASE_TODAY": "2026-11-11"}).returncode == 0
    result = preflight("changelog-date", "1.1", env={"RELEASE_TODAY": "2026-11-11"})
    assert result.returncode == 1 and "has no '## [1.1] - YYYY-MM-DD' heading" in result.stderr


@pytest.fixture
def release_repo(tmp_path):
    """A repository with main at 1.1.0 and a side branch whose commit never reached main."""
    git = shutil.which("git")
    if git is None:
        pytest.skip("git isn't installed here")
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*args):
        result = subprocess.run(  # noqa: S603
            [git, "-c", "user.name=t", "-c", "user.email=t@e2e.test", "-c", "commit.gpgsign=false", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def write(api_version, chart_version):
        (repo / "apps" / "api").mkdir(parents=True, exist_ok=True)
        (repo / "deploy" / "helm" / "softwarden").mkdir(parents=True, exist_ok=True)
        (repo / "apps" / "api" / "pyproject.toml").write_bytes(
            f'[project]\nname = "softwarden-api"\nversion = "{api_version}"\n\n[tool.x]\nversion = "9.9.9"\n'.encode()
        )
        (repo / "deploy" / "helm" / "softwarden" / "Chart.yaml").write_bytes(
            f'apiVersion: v2\nversion: 3.0.0\nappVersion: "{chart_version}"\n'.encode()
        )

    g("init", "-q", "-b", "main")
    write("1.0.0", "1.0.0")
    g("add", "-A")
    g("commit", "-q", "-m", "1.0.0")
    first = g("rev-parse", "HEAD")
    g("checkout", "-q", "-b", "side")
    (repo / "side.txt").write_bytes(b"x\n")
    g("add", "-A")
    g("commit", "-q", "-m", "side")
    side = g("rev-parse", "HEAD")
    g("checkout", "-q", "main")
    write("1.1.0rc1", "1.1.0-rc.1")
    g("add", "-A")
    g("commit", "-q", "-m", "rc")
    rc = g("rev-parse", "HEAD")
    return {"dir": repo, "first": first, "side": side, "rc": rc, "write": write}


def test_preflight_repo_accepts_a_main_commit_whose_versions_match(preflight, release_repo):
    result = preflight("repo", "1.1.0-rc.1", release_repo["rc"], "main", cwd=release_repo["dir"])
    assert result.returncode == 0, result.stderr  # PEP 440 1.1.0rc1 in pyproject is the same version
    # An older commit on main is fine too (its own versions are checked against the files checked out).


def test_preflight_repo_refuses_a_commit_that_isnt_on_main(preflight, release_repo):
    result = preflight("repo", "1.1.0-rc.1", release_repo["side"], "main", cwd=release_repo["dir"])
    assert result.returncode == 1 and "isn't on main" in result.stderr


def test_preflight_repo_refuses_versions_that_differ_from_the_tag(preflight, release_repo):
    result = preflight("repo", "1.1.0", release_repo["rc"], "main", cwd=release_repo["dir"])
    assert result.returncode == 1 and "pyproject.toml says version 1.1.0rc1, the tag says 1.1.0" in result.stderr
    release_repo["write"]("1.1.0", "1.0.9")
    result = preflight("repo", "1.1.0", release_repo["rc"], "main", cwd=release_repo["dir"])
    assert result.returncode == 1 and "Chart.yaml says appVersion 1.0.9, the tag says 1.1.0" in result.stderr


def test_preflight_repo_refuses_an_unknown_main(preflight, release_repo):
    result = preflight("repo", "1.1.0-rc.1", release_repo["rc"], "origin/main", cwd=release_repo["dir"])
    assert result.returncode == 1 and "origin/main isn't known here" in result.stderr


@pytest.fixture
def imagetools(tmp_path):
    """A stand-in for `docker buildx imagetools inspect`: answers from a file of published references."""
    published = tmp_path / "published.txt"
    published.write_bytes(b"")
    stub = tmp_path / "inspect.sh"
    stub.write_bytes(
        b'#!/usr/bin/env bash\nref=$1\nif [ -n "${STUB_ERROR:-}" ]; then echo "$STUB_ERROR" >&2; exit 1; fi\n'
        b'if grep -qxF "$ref" "$(dirname "$0")/published.txt"; then echo "Name: $ref"; exit 0; fi\n'
        b'echo "ERROR: $ref: not found" >&2; exit 1\n'
    )
    # A stand-in for `gh api /orgs|users/<owner>/packages/container/<name>`: STUB_GH_ORGS and STUB_GH_USERS are
    # "exists" (the default), "404" or anything else (another error). Each call is logged.
    gh = tmp_path / "gh.sh"
    gh.write_bytes(
        b'#!/usr/bin/env bash\necho "$2" >> "$(dirname "$0")/gh-calls.txt"\n'
        b"case $2 in /orgs/*) answer=${STUB_GH_ORGS:-exists} ;; *) answer=${STUB_GH_USERS:-exists} ;; esac\n"
        b'case $answer in exists) echo "{}"; exit 0 ;; 404) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;; esac\n'
        b'echo "gh: Server Error (HTTP 500)" >&2; exit 1\n'
    )
    return {
        "env": {"RELEASE_IMAGETOOLS": f"bash {stub.as_posix()}", "RELEASE_GH": f"bash {gh.as_posix()}"},
        "published": published,
        "gh_calls": tmp_path / "gh-calls.txt",
    }


def test_preflight_unpublished_passes_when_neither_image_has_the_version(preflight, imagetools):
    result = preflight(
        "unpublished", "ghcr.io/o/softwarden:1.0.0", "ghcr.io/o/softwarden-mcp:1.0.0", env=imagetools["env"]
    )
    assert result.returncode == 0, result.stderr
    assert "ghcr.io/o/softwarden-mcp:1.0.0: not published yet" in result.stdout


def test_preflight_unpublished_refuses_a_version_already_in_the_registry(preflight, imagetools):
    imagetools["published"].write_bytes(b"ghcr.io/o/softwarden-mcp:1.0.0\n")
    result = preflight(
        "unpublished", "ghcr.io/o/softwarden:1.0.0", "ghcr.io/o/softwarden-mcp:1.0.0", env=imagetools["env"]
    )
    assert result.returncode == 1 and "ghcr.io/o/softwarden-mcp:1.0.0 is already published" in result.stderr


def test_preflight_unpublished_refuses_when_the_registry_cant_say(preflight, imagetools):
    env = {**imagetools["env"], "STUB_ERROR": "ERROR: unexpected status: 401 Unauthorized"}
    result = preflight("unpublished", "ghcr.io/o/softwarden:1.0.0", env=env)
    assert result.returncode == 1 and "can't tell whether ghcr.io/o/softwarden:1.0.0 exists" in result.stderr


DENIED = "ERROR: failed to authorize: failed to fetch anonymous token: unexpected status: 403 Forbidden (denied)"


def test_preflight_unpublished_passes_the_first_release_when_the_package_doesnt_exist(preflight, imagetools):
    """Before the first push GHCR may answer "denied" for a package that doesn't exist; the packages API then
    answers 404 under the owner as an organization and as a user."""
    env = {**imagetools["env"], "STUB_ERROR": DENIED, "STUB_GH_ORGS": "404", "STUB_GH_USERS": "404"}
    result = preflight("unpublished", "ghcr.io/o/softwarden:1.0.0", "ghcr.io/o/softwarden-mcp:1.0.0", env=env)
    assert result.returncode == 0, result.stderr
    assert "ghcr.io/o/softwarden-mcp:1.0.0: not published yet (no such package: the first release)" in result.stdout
    calls = imagetools["gh_calls"].read_text().split()
    assert "/orgs/o/packages/container/softwarden" in calls and "/users/o/packages/container/softwarden-mcp" in calls


@pytest.mark.parametrize(("orgs", "users"), [("exists", "404"), ("404", "exists"), ("404", "500"), ("500", "404")])
def test_preflight_unpublished_refuses_denied_when_the_package_may_exist(preflight, imagetools, orgs, users):
    env = {**imagetools["env"], "STUB_ERROR": DENIED, "STUB_GH_ORGS": orgs, "STUB_GH_USERS": users}
    result = preflight("unpublished", "ghcr.io/o/softwarden:1.0.0", env=env)
    assert result.returncode == 1 and "can't tell whether ghcr.io/o/softwarden:1.0.0 exists" in result.stderr


TAGS = "v1.0.0\nv1.0.5\nv1.1.0\nv1.1.1-rc.1\nv2.0.0-rc.1\nv1.10.0-beta.1\nnot-a-version\nv01.0.0\n"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.1.1", ["1.1.1", "1.1", "latest"]),  # the highest stable release (its own tag isn't pushed yet)
        ("1.1.0", ["1.1.0", "1.1", "latest"]),  # re-computed with its own tag in the list
        ("1.0.6", ["1.0.6", "1.0"]),  # a patch to an older line: its line's tag, never latest
        ("1.0.3", ["1.0.3"]),  # lower than 1.0.5: neither
        ("2.0.0", ["2.0.0", "2.0", "latest"]),  # pre-releases don't count
        ("1.2.0-rc.1", ["1.2.0-rc.1"]),  # a pre-release gets only its own tag
        ("1.10.0", ["1.10.0", "1.10", "latest"]),  # compared as numbers, not text
    ],
)
def test_preflight_tags_moves_latest_only_for_the_highest_stable_release(preflight, version, expected):
    result = preflight("tags", version, stdin=TAGS)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == expected


def test_preflight_script_is_shellchecked_in_ci():
    deploy = steps_text(workflow(CI)["jobs"]["deploy-config"])
    assert "ops/ci/*.sh" in deploy  # shellcheck covers release-preflight.sh


def test_docs_and_helm_name_the_images_the_way_release_publishes_them():
    install = (REPO / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    runbook = (REPO / "docs" / "04-development" / "upgrade-runbook.md").read_text(encoding="utf-8")
    values = yaml.safe_load((REPO / "deploy" / "helm" / "softwarden" / "values.yaml").read_text(encoding="utf-8"))
    assert "SOFTWARDEN_IMAGE=ghcr.io/softwarden/softwarden:1.0.0" in install
    assert "git clone https://github.com/Softwarden/softwarden.git" in install
    assert "ghcr.io/softwarden/softwarden:<new tag>" in runbook
    assert values["image"]["repository"] == "ghcr.io/softwarden/softwarden"
    assert values["mcp"]["image"]["repository"] == "ghcr.io/softwarden/softwarden-mcp"
    for text in (install, runbook):
        # The repository is Softwarden/softwarden: no placeholder is left for a reader to fill.
        assert "<owner>" not in text and "OWNER" not in text and "<repository URL>" not in text


def test_the_changelog_exists_in_the_format_the_release_reads():
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.startswith("# Changelog")
    assert re.search(r"^## \[Unreleased\]", text, re.MULTILINE)


SAMPLE = """# Changelog

## [Unreleased]

- later

## [1.0.10] - 2026-12-01

- not this one

## [1.0.0] - 2026-11-08


### Added
- one
- two


## 0.9.0
- old
"""


@pytest.fixture
def run_section(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash isn't installed here")
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_bytes(SAMPLE.encode())

    def run(version):
        return subprocess.run(  # noqa: S603
            [bash, str(CHANGELOG_SECTION), version, str(changelog)], capture_output=True, text=True
        )

    return run


def test_changelog_section_prints_one_release_without_its_heading(run_section):
    result = run_section("v1.0.0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.replace("\r\n", "\n") == "### Added\n- one\n- two\n"
    assert run_section("0.9.0").stdout.strip() == "- old"  # a heading without brackets


def test_changelog_section_refuses_a_version_without_notes(run_section):
    result = run_section("1.0.1")
    assert result.returncode == 1 and result.stdout == ""
    assert "no notes for 1.0.1" in result.stderr


def test_load_config_never_lets_backup_env_override_the_compose_target():
    """ops/backup.env is loaded by every ops script; the Compose variables a caller set must survive it."""
    common = (REPO / "ops" / "common.sh").read_text(encoding="utf-8")
    saved = common[common.index("load_config() {") : common.index('. "$script_dir/backup.env"')]
    for name in ("COMPOSE_PROJECT_NAME", "COMPOSE_FILE", "COMPOSE_PROFILES", "SOFTWARDEN_DIR"):
        assert name in saved, name


def test_ci_cancels_superseded_runs_only_on_pull_requests():
    assert workflow(CI)["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_a_website_only_change_runs_the_site_build_and_not_the_app_ci():
    """ci.yml skips pushes and pull requests that touch only site/, but a release (workflow_call) runs all of it.
    site.yml builds the website whenever it or what it renders (docs/, CHANGELOG.md) changes."""
    ci = workflow(CI)["on"]
    assert ci["push"]["paths-ignore"] == ci["pull_request"]["paths-ignore"] == ["site/**"]
    assert ci["workflow_call"] is None
    site = workflow(SITE)
    for event in ("push", "pull_request"):
        assert {"site/**", "docs/**", "CHANGELOG.md"} <= set(site["on"][event]["paths"]), event
    assert site["permissions"] == {"contents": "read"}
    steps = steps_text(site["jobs"]["build"])
    assert "npm ci" in steps and "npm run build" in steps
    assert site["jobs"]["build"]["defaults"]["run"]["working-directory"] == "site"


# ---- the release's version, everywhere a version lives (docs/DEVELOPMENT.md, "Cutting a release", step 1) --------------

# A release's date, or the placeholder it keeps until its tag day (docs/DEVELOPMENT.md, "Cutting a release", step 1); release.yml refuses to
# publish a heading that still has the placeholder.
DATE_PLACEHOLDER = "unreleased (set on tag day)"
RELEASE_HEADING = re.compile(
    r"^## \[(?P<version>\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)\] - (?:\d{4}-\d{2}-\d{2}|"
    + re.escape(DATE_PLACEHOLDER)
    + r")$",
    re.MULTILINE,
)


def changelog():
    return (REPO / "CHANGELOG.md").read_text(encoding="utf-8")


def releases():
    """Every `## [X.Y.Z] - YYYY-MM-DD` section's version in CHANGELOG.md, newest first, all below [Unreleased]."""
    text = changelog()
    matches = list(RELEASE_HEADING.finditer(text))
    assert matches, "CHANGELOG.md has no release section"
    unreleased = re.search(r"^## \[Unreleased\]$", text, re.MULTILINE)
    assert unreleased and unreleased.start() < matches[0].start(), "[Unreleased] must stay above the newest release"
    return [match["version"] for match in matches]


def newest_release():
    """What every version string must say: the newest section, a pre-release (1.1.0-rc.1) included."""
    return releases()[0]


def newest_stable_release():
    """What INSTALL.md pins: the newest section without a pre-release suffix. A release candidate is tagged and
    published, but the install guide keeps telling new installations to run the last stable release."""
    return next(version for version in releases() if "-" not in version)


def same_version(found, release):
    """Equal as PEP 440 versions, so uv.lock's normalised `1.1.0rc1` matches the changelog's SemVer `1.1.0-rc.1`
    (packaging normalises both); a string that isn't a PEP 440 version must match exactly."""
    try:
        return Version(str(found)) == Version(release)
    except InvalidVersion:
        return str(found) == release


def test_same_version_compares_semver_and_pep_440_spellings():
    assert same_version("1.1.0rc1", "1.1.0-rc.1")
    assert same_version("1.0.0", "1.0.0")
    assert not same_version("1.1.0rc2", "1.1.0-rc.1")
    assert not same_version("1.0.0", "1.1.0-rc.1")
    assert not same_version("unknown", "1.0.0")


def locked_version(lock, name):
    """The version uv.lock records for the project's own package."""
    packages = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
    return next(package["version"] for package in packages if package["name"] == name)


def test_every_version_string_is_the_newest_changelog_release():
    from django.conf import settings

    from softwarden import ops

    release = newest_release()
    web = json.loads((REPO / "apps" / "web" / "package.json").read_text(encoding="utf-8"))
    web_lock = json.loads((REPO / "apps" / "web" / "package-lock.json").read_text(encoding="utf-8"))
    mcp_init = (REPO / "mcp" / "softwarden_mcp" / "__init__.py").read_text(encoding="utf-8")
    mcp_version = re.search(r'^__version__ = "([^"]+)"$', mcp_init, re.MULTILINE)
    chart = yaml.safe_load((REPO / "deploy" / "helm" / "softwarden" / "Chart.yaml").read_text(encoding="utf-8"))
    found = {
        "apps/api/pyproject.toml": tomllib.loads((REPO / "apps" / "api" / "pyproject.toml").read_text("utf-8"))[
            "project"
        ]["version"],
        "apps/api/uv.lock": locked_version(REPO / "apps" / "api" / "uv.lock", "softwarden-api"),
        "/healthz/ops version": ops.version(),
        "SPECTACULAR_SETTINGS VERSION": settings.SPECTACULAR_SETTINGS["VERSION"],
        "mcp/pyproject.toml": tomllib.loads((REPO / "mcp" / "pyproject.toml").read_text("utf-8"))["project"]["version"],
        "mcp/uv.lock": locked_version(REPO / "mcp" / "uv.lock", "softwarden-mcp"),
        "softwarden_mcp.__version__": mcp_version[1] if mcp_version else None,
        "apps/web/package.json": web["version"],
        "apps/web/package-lock.json": web_lock["version"],
        "apps/web/package-lock.json packages['']": web_lock["packages"][""]["version"],
        "Chart.yaml appVersion": chart["appVersion"],
    }
    wrong = {where: version for where, version in found.items() if not same_version(version, release)}
    assert not wrong, f"CHANGELOG.md's newest release is {release}; these differ: {wrong}"


def test_install_guide_pins_the_newest_stable_release():
    release = newest_stable_release()
    install = (REPO / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    assert f"SOFTWARDEN_IMAGE=ghcr.io/softwarden/softwarden:{release}" in install
    assert f"git checkout v{release}" in install


def section_in_python(version):
    """The section's body as changelog-section.sh should print it: after its heading, up to the next `## ` heading,
    without blank lines at either end."""
    text = changelog().replace("\r\n", "\n")
    heading = next(m for m in RELEASE_HEADING.finditer(text) if m["version"] == version)
    rest = text[heading.end() :]
    following = re.search(r"^## ", rest, re.MULTILINE)
    return (rest[: following.start()] if following else rest).strip("\n") + "\n"


@pytest.fixture
def real_section():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash isn't installed here")

    def run(version):
        result = subprocess.run(  # noqa: S603
            [bash, str(CHANGELOG_SECTION), f"v{version}", str(REPO / "CHANGELOG.md")], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        return result.stdout.replace("\r\n", "\n")

    return run


def test_changelog_section_extracts_the_newest_release_from_the_real_changelog(real_section):
    release = newest_release()
    notes = real_section(release)
    assert notes.strip(), "the newest release has no notes"
    assert not re.search(r"^## ", notes, re.MULTILINE)  # no heading of its own, and no other release's
    assert notes == section_in_python(release)  # exactly up to the next release, none of its text
    older = releases()[1:]
    if older:
        assert section_in_python(older[0]).strip() not in notes


def test_the_1_0_0_notes_have_the_sections_the_release_package_wrote(real_section):
    notes = real_section("1.0.0")
    for heading in ("### Added", "#### Self-host operations", "### Known limitations"):
        assert re.search(rf"^{re.escape(heading)}$", notes, re.MULTILINE), heading


# ---- pinned actions and images -------------------------------------------------------

CHECK_PINS = REPO / "ops" / "ci" / "check-pins.sh"
SHA = "11d5960a326750d5838078e36cf38b85af677262"
DIGEST = "sha256:" + "a" * 64
PINNED_WORKFLOW = f"""name: x
on: push
jobs:
  a:
    runs-on: ubuntu-24.04
    services:
      postgres:
        image: postgres:17.11-trixie@{DIGEST}  # compose.yaml's
    strategy:
      matrix:
        include:
          - image: softwarden
    steps:
      - uses: actions/checkout@{SHA}  # v4.4.0
      - uses: "docker/build-push-action@{SHA}" # v6.19.2
      - run: uvx pip-audit@2.10.1 -r x
  b:
    uses: ./.github/workflows/ci.yml
"""
PINNED_DOCKERFILE = f"""FROM node:22.16-bookworm-slim@{DIGEST} AS web
RUN true
FROM python:3.13.18-slim-bookworm@{DIGEST}
COPY --from=ghcr.io/astral-sh/uv:0.9.18@{DIGEST} /uv /usr/local/bin/uv
COPY --from=web /src/dist /app/web
COPY --from=0 /src /src
RUN --mount=type=bind,from=cacerts,target=/tmp/ca-certs true
"""
PINNED_COMPOSE = f"""services:
  app:
    image: ${{SOFTWARDEN_IMAGE:-softwarden:local}}
  mcp:
    image: ${{SOFTWARDEN_MCP_IMAGE:-softwarden-mcp:local}}
  db:
    image: postgres:17.11-trixie@{DIGEST}
  proxy:
    image: "caddy:2.11.9@{DIGEST}"  # pinned
"""


@pytest.fixture
def pins(tmp_path):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash isn't installed here")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "mcp").mkdir()
    files = {
        ".github/workflows/ci.yml": PINNED_WORKFLOW,
        "Dockerfile": PINNED_DOCKERFILE,
        "mcp/Dockerfile": f"FROM python:3.13.18-slim-bookworm@{DIGEST}\n",
        "compose.yaml": PINNED_COMPOSE,
    }

    def run(**changes):
        for name, text in {
            **files,
            **{k.replace("__", "/").replace("_DOT_", "."): v for k, v in changes.items()},
        }.items():
            (tmp_path / name).write_bytes(text.encode())
        return subprocess.run([bash, str(CHECK_PINS), str(tmp_path)], capture_output=True, text=True)  # noqa: S603

    return run


def test_check_pins_passes_on_the_repository():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash isn't installed here")
    result = subprocess.run([bash, str(CHECK_PINS), str(REPO)], capture_output=True, text=True)  # noqa: S603
    assert result.returncode == 0, result.stdout + result.stderr


def test_check_pins_passes_on_pinned_files(pins):
    result = pins()
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        (f"actions/checkout@{SHA}  # v4.4.0", "actions/checkout@v4", "actions/checkout@v4 isn't pinned to a full"),
        (f"actions/checkout@{SHA}  # v4.4.0", "actions/checkout@main", "actions/checkout@main isn't pinned"),
        (f"actions/checkout@{SHA}  # v4.4.0", f"actions/checkout@{SHA[:7]}  # v4", "isn't pinned to a full commit SHA"),
        (f"actions/checkout@{SHA}  # v4.4.0", f"actions/checkout@{SHA}", "has no '# vX.Y.Z' comment"),
        (f"postgres:17.11-trixie@{DIGEST}  # compose", "postgres:17.11-trixie  # compose", "has no @sha256: digest"),
        ("uvx pip-audit@2.10.1", "uvx pip-audit", "uvx tool without a version"),
    ],
)
def test_check_pins_refuses_an_unpinned_workflow_reference(pins, old, new, error):
    assert old in PINNED_WORKFLOW
    result = pins(_DOT_github__workflows__ci_DOT_yml=PINNED_WORKFLOW.replace(old, new))
    assert result.returncode == 1 and error in result.stdout, result.stdout


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (f"node:22.16-bookworm-slim@{DIGEST} AS web", "node:22.16-bookworm-slim AS web"),
        (f"python:3.13.18-slim-bookworm@{DIGEST}", "python:3.13.18-slim-bookworm"),
        (f"ghcr.io/astral-sh/uv:0.9.18@{DIGEST}", "ghcr.io/astral-sh/uv:0.9.18"),
        ("COPY --from=web", "COPY --from=nginx:1.27"),  # an image, not a stage
    ],
)
def test_check_pins_refuses_a_dockerfile_image_without_a_digest(pins, old, new):
    assert old in PINNED_DOCKERFILE
    result = pins(Dockerfile=PINNED_DOCKERFILE.replace(old, new))
    assert result.returncode == 1 and "has no @sha256: digest" in result.stdout, result.stdout


def test_check_pins_refuses_a_compose_image_without_a_digest(pins):
    result = pins(compose_DOT_yaml=PINNED_COMPOSE.replace(f"caddy:2.11.9@{DIGEST}", "caddy:2.11.9"))
    assert result.returncode == 1 and "image caddy:2.11.9 has no @sha256: digest" in result.stdout, result.stdout


SIGNED_SOURCE = (
    '      - run: echo "deb [signed-by=/usr/share/keyrings/pgdg.asc] https://apt.postgresql.org/pub/repos/apt '
    'noble-pgdg main" | sudo tee /etc/apt/sources.list.d/pgdg.list\n'
)


@pytest.mark.parametrize(
    ("line", "error"),
    [
        (SIGNED_SOURCE.replace("[signed-by=/usr/share/keyrings/pgdg.asc] ", ""), "apt source without signed-by="),
        (
            "      - run: curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo gpg --dearmor -o k.gpg\n",
            "signing key fetched at run time",
        ),
        (
            "      - run: wget -qO- https://example.test/key.asc | sudo apt-key add -\n",
            "signing key fetched at run time",
        ),
        (  # a key downloaded to a file, then named in signed-by=
            "      - run: sudo curl -fsSLo /usr/share/keyrings/pgdg.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc\n"
            + SIGNED_SOURCE,
            "signing key fetched at run time",
        ),
        (
            "      - run: sudo wget -O /etc/apt/keyrings/x.gpg https://example.test/x\n",
            "signing key fetched at run time",
        ),
        ("      - run: sudo add-apt-repository -y ppa:deadsnakes/ppa\n", "signing key fetched at run time"),
    ],
)
def test_check_pins_refuses_an_apt_source_without_a_committed_key(pins, line, error):
    """The api job's postgresql-client-17 comes from PGDG signed by docker/pgdg's committed key."""
    assert pins(_DOT_github__workflows__ci_DOT_yml=PINNED_WORKFLOW + SIGNED_SOURCE).returncode == 0
    result = pins(_DOT_github__workflows__ci_DOT_yml=PINNED_WORKFLOW + line)
    assert result.returncode == 1 and error in result.stdout, result.stdout
    unsigned = PINNED_DOCKERFILE + 'RUN echo "deb https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > x\n'
    assert "apt source without signed-by=" in pins(Dockerfile=unsigned).stdout


def test_the_api_job_runs_the_real_pg_dump_backup_test():
    """The api job installs postgresql-client-17 from PGDG with the committed key (the Dockerfile's), so
    tests/test_backup.py's full backup with the real pg_dump runs there; it fails rather than skips when CI is set."""
    api = workflow(CI)["jobs"]["api"]
    names = [step.get("name", step.get("run", "")) for step in api["steps"]]
    install = names.index("PostgreSQL 17 client (pg_dump, pg_restore)")
    assert install < next(i for i, n in enumerate(names) if str(n).startswith("uv run pytest"))
    text = api["steps"][install]["run"]
    assert "docker/pgdg/ACCC4CF8.asc" in text and "signed-by=" in text and "noble-pgdg main" in text
    assert "apt-get install -y --no-install-recommends postgresql-client-17" in text
    assert 'echo /usr/lib/postgresql/17/bin >> "$GITHUB_PATH"' in text
    backup_tests = (REPO / "apps" / "api" / "tests" / "test_backup.py").read_text(encoding="utf-8")
    assert re.search(r'if os\.environ\.get\("CI"\):\r?\n\s+pytest\.fail\(', backup_tests)


def test_the_compose_job_restores_a_worker_backup():
    """`backup_now` in the worker, then ops/restore.sh --rehearse on that set, on the built stack."""
    job = workflow(CI)["jobs"]["compose-first-run"]
    text = steps_text(job)
    assert "bash ops/ci/inapp-backup-rehearsal.sh" in text
    assert text.index("inapp-backup-rehearsal.sh") < text.index("down --volumes")
    script = (REPO / "ops" / "ci" / "inapp-backup-rehearsal.sh").read_text(encoding="utf-8")
    for part in (
        "dc exec -T worker python manage.py backup_now --inline",
        'dc cp "worker:/data/backups/daily/$set_name"',
        "dc cp worker:/data/secrets/backup_key",
        'bash "$repo/ops/restore.sh" --rehearse "$work/$set_name/$set_name.dump.enc"',
        "attachment files are present and match",
        "Audit events intact",
    ):
        assert part in script, part
    assert "set -euo pipefail" in script and "exec </dev/null" in script


def test_every_action_is_pinned_the_way_the_review_checks():
    """Pinned actions and images: no `uses:` on a version tag or a branch; no FROM without a digest."""
    for path in (CI, RELEASE, SITE):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"uses: [^@\s]+@(v[0-9]|main|master)", text), path
        for ref in re.findall(r"uses: ([^\s#]+)", text):
            assert ref.startswith("./") or re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), ref
    for dockerfile in ("Dockerfile", "mcp/Dockerfile"):
        for line in (REPO / dockerfile).read_text(encoding="utf-8").splitlines():
            if line.startswith("FROM "):
                assert "@sha256:" in line, line
    assert "bash ops/ci/check-pins.sh" in steps_text(workflow(CI)["jobs"]["deploy-config"])
    dependabot = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    ecosystems = {update["package-ecosystem"] for update in dependabot["updates"]}
    assert {"github-actions", "docker", "docker-compose", "uv", "npm"} <= ecosystems
