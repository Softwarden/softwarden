"""Every relative link and anchor in the repository's Markdown resolves.

The check itself is ops/ci/check-doc-links.py (standard library and git only), so it also runs without the API's
environment: `python3 ops/ci/check-doc-links.py`. Moving or renaming a document fails here until its links follow.
"""

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ops" / "ci" / "check-doc-links.py"


def load():
    spec = importlib.util.spec_from_file_location("check_doc_links", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


needs_git = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs a git checkout of the repository"
)


@needs_git
def test_no_broken_links_or_anchors():
    assert load().broken_links() == []


def test_the_checker_finds_a_broken_link_and_anchor(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("needs git")
    subprocess.run([git, "init", "-q"], cwd=tmp_path, check=True)  # noqa: S603
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text(
        "# Title\n\n## Quick start\n\n[ok](docs/A.md) [anchor](#quick-start) [web](https://example.com)\n"
        "[gone](docs/B.md) [bad anchor](docs/A.md#nowhere)\n```\n[in a fence](nowhere.md)\n```\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "A.md").write_text("# A\n\n[up](../README.md#quick-start)\n", encoding="utf-8")
    subprocess.run([git, "add", "-A"], cwd=tmp_path, check=True)  # noqa: S603
    assert load().broken_links(tmp_path) == [
        "README.md: docs/B.md (no such file)",
        "README.md: docs/A.md#nowhere (no such anchor)",
    ]
