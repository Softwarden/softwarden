"""ops/ci/check-dco.sh, the Developer Certificate of Origin check CI runs on a pull request's commits.

A commit passes when it carries `Signed-off-by:` with its author's email, as `git commit -s` writes it; merges are
skipped. Each case runs the script on a throwaway repository.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops" / "ci" / "check-dco.sh"
GIT = shutil.which("git") or "git"
BASH = shutil.which("bash") or "bash"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash"
)


def git(cwd: Path, *args: str) -> str:
    env_args = ["-c", "user.name=Ada Example", "-c", "user.email=ada@example.test", "-c", "commit.gpgsign=false"]
    return subprocess.run([GIT, *env_args, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout  # noqa: S603


def commit(cwd: Path, name: str, message: str, author: str | None = None) -> None:
    (cwd / name).write_text(name, encoding="utf-8")
    git(cwd, "add", name)
    extra = ["--author", author] if author else []
    git(cwd, "commit", "-q", "-m", message, *extra)


def run(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [BASH, SCRIPT.as_posix(), "base", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q")
    commit(tmp_path, "a", "base commit")
    git(tmp_path, "tag", "base")
    return tmp_path


def test_signed_off_commits_pass(repo: Path):
    commit(repo, "b", "Add b\n\nSigned-off-by: Ada Example <ada@example.test>")
    commit(repo, "c", "Add c\n\nSigned-off-by: Ada Example <ADA@example.test>")
    result = run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "every commit" in result.stdout


def test_a_commit_without_sign_off_fails(repo: Path):
    commit(repo, "b", "Add b\n\nSigned-off-by: Ada Example <ada@example.test>")
    commit(repo, "c", "Add c without a sign-off")
    result = run(repo)
    assert result.returncode == 1
    assert '"Add c without a sign-off" has no Signed-off-by line' in result.stdout
    assert "1 commit(s)" in result.stdout


def test_dependabot_commits_are_skipped(repo: Path):
    commit(repo, "b", "Bump a dependency", author="dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>")
    result = run(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_only_dependabot_is_skipped(repo: Path):
    commit(repo, "b", "Bump a dependency", author="Other Bot <other[bot]@users.noreply.github.com>")
    result = run(repo)
    assert result.returncode == 1
    assert "has no Signed-off-by line" in result.stdout


def test_a_sign_off_by_someone_else_fails(repo: Path):
    commit(repo, "b", "Add b\n\nSigned-off-by: Bob Other <bob@example.test>", author="Ada Example <ada@example.test>")
    result = run(repo)
    assert result.returncode == 1
    assert "not signed off by its author <ada@example.test>" in result.stdout
