"""No build-process labels in the repository: decision ids in parentheses (a D and a number, alone or last in a list),
milestone and increment item ids (an M and a digit, or 1A to 1C, then -D and a digit), epic ids (an E, a number, a dash
and a number), user story ids (US, a dash and a digit), build phase labels (Phase and a number), work-package names for
the discovery sources (Sources and A or B) or the names of the earlier iterations (IT and 1 or 2). The samples below
are assembled from pieces, so this file passes its own check.

They pointed at planning records that aren't part of the repository, so a reader can't follow them; a comment says
its reason in words instead. Every file git tracks is read (code, comments, docs, configuration and test data),
except the lockfiles, which only list packages.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LABELS = re.compile(
    r"\(D\d+\)|, D\d+\)|\bM\d-D\d|\b1[A-C]-D\d|\bE\d{1,2}-\d\b|\bUS-\d|Phase \d+|Sources [AB]\b|\bIT[12]\b"
)
SKIPPED = {"package-lock.json", "uv.lock"}

needs_git = pytest.mark.skipif(
    shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs a git checkout of the repository"
)


def labels_in(text: str) -> list[str]:
    return [f"line {number}: {match.group(0)}" for number, line in enumerate(text.splitlines(), start=1)
            for match in LABELS.finditer(line)]  # fmt: skip


def tracked_files() -> list[str]:
    git = shutil.which("git")
    assert git is not None
    out = subprocess.run([git, "ls-files", "-z"], cwd=REPO, capture_output=True, check=True).stdout  # noqa: S603
    return [name for name in out.decode("utf-8").split("\0") if name and Path(name).name not in SKIPPED]


@needs_git
def test_no_tracked_file_carries_a_build_label():
    found = {}
    for name in tracked_files():
        path = REPO / name
        if not path.is_file():  # deleted in the working tree
            continue
        data = path.read_bytes()
        if b"\0" in data:  # images, fonts and other binaries
            continue
        problems = labels_in(data.decode("utf-8", errors="replace"))
        if problems:
            found[name] = problems
    assert found == {}


def test_the_check_catches_each_label():
    decision, item, phase, first, second = "(" + "D79)", "M2" + "-D4", "Phase" + " 11", "I" + "T1", "I" + "T2"
    assert labels_in(f"refused {decision}.") == [f"line 1: {decision}"]
    assert labels_in(f"ok\nat the size {item} asks for") == [f"line 2: {item}"]
    assert labels_in(f"# {phase}: ticket links") == [f"line 1: {phase}"]
    assert labels_in(f"ported from {first} and {second}") == [f"line 1: {first}", f"line 1: {second}"]
    in_list, increment, epic, story, package = (
        ", " + "D9)",
        "1C" + "-D2",
        "E30" + "-3",
        "US" + "-1a-06",
        "Sources" + " B",
    )
    assert labels_in(f"secrets (A11{in_list}") == [f"line 1: {in_list}"]
    assert labels_in(f"API tokens ({increment})") == [f"line 1: {increment}"]
    assert labels_in(f"backups ({epic})") == [f"line 1: {epic}"]
    assert labels_in(f"the person panel ({story})") == [f"line 1: {story[:4]}"]
    assert labels_in(f"# {package}: the azure source") == [f"line 1: {package}"]
    # Words that only look alike are fine: SKU names, SVG paths, figures and identifiers.
    assert labels_in("SKU E5, 2 E3 seats, USD 10, US-East, Sources Azure, (A, D) and (x, Dana)") == []
    assert labels_in('Microsoft 365 E5, d="M2 7h34", phase in, (Dana), IT12, ITEM2, edit1') == []
