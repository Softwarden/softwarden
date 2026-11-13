"""Writes the image's THIRD_PARTY_NOTICES: every Python package the image installs and
every npm package the SPA is built from, each with its declared license and the license texts it ships.

    python third_party_notices.py --requirements req.txt --npm npm.txt --out THIRD_PARTY_NOTICES.txt

--requirements is `uv export --frozen --no-dev --no-hashes --no-emit-project` (the runtime set, apps/api/uv.lock);
each package in it is read from the installed environment with importlib.metadata. A package missing from the
environment fails the build, unless its environment marker is false here (`; sys_platform == 'win32'` in a Linux
image): uv exports for every platform, and such a package is never installed on this one. The marker is evaluated with
packaging.markers when the environment has it, else by marker_excludes() below, which reads `sys_platform`,
`platform_system`, `implementation_name` and `os_name` comparisons (==, !=) joined by `and` / `or`; a marker it can't
read counts as not excluding, so the package fails the build rather than being left out. --npm is the output of apps/web/scripts/third-party-notices.mjs from the web stage.
Standard library only: the Dockerfile runs it with the image's own Python, never copied into the image.
"""

from __future__ import annotations

import argparse
import os
import platform
import re
import sys
from importlib import metadata

RULE = "=" * 78
LICENSE_FILE = re.compile(r"(^|/)(licen[cs]e|copying|notice|copyright|authors)([-_.][^/]*)?$", re.I)
REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?==([^\s;\\]+)\s*(;[^\\]*)?")

HEADER = """THIRD-PARTY NOTICES

Softwarden is licensed under the Apache License 2.0. This image also contains the third-party software listed below, each under its own license. For each
package: its name and version, the license it declares, where its source is, and the license and notice texts it
ships.

Part 1 lists the Python packages the API and the worker run on (apps/api/uv.lock, without development tools).
Part 2 lists the npm packages the web interface is built from (apps/web/package-lock.json, without development tools);
the built interface contains parts of them.

The operating system packages of the Debian base image (Python {python}, the PostgreSQL client, OpenSSL and the rest)
carry their licenses in the image under /usr/share/doc/<package>/copyright.

Generated when the image was built by docker/third_party_notices.py.
"""


def parse_requirements(text: str) -> list[tuple[str, str, str]]:
    """(name, version, environment marker or "") for each pinned requirement in `uv export` output."""
    found = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        m = REQUIREMENT.match(line)
        if m:
            found.append((m[1], m[3], (m[4] or "").lstrip(";").strip()))
    return found


MARKER_CLAUSE = re.compile(r"""^\s*(sys_platform|platform_system|implementation_name|os_name)\s*(==|!=)\s*(['"])([^'"]*)\3\s*$""")


def this_environment() -> dict[str, str]:
    return {
        "sys_platform": sys.platform,
        "platform_system": platform.system(),
        "implementation_name": sys.implementation.name,
        "os_name": os.name,
    }


def marker_excludes(marker: str, env: dict[str, str] | None = None) -> bool:
    """Whether an environment marker is false in this environment, so the package isn't installed here. Without
    packaging (the image's runtime set has none), only the simple markers uv writes for platform-only packages are read;
    anything else answers False, so a missing package with it fails the build."""
    if not marker:
        return False
    env = env or this_environment()
    try:
        from packaging.markers import InvalidMarker, Marker
    except ImportError:
        pass
    else:
        try:
            return not Marker(marker).evaluate(env)
        except InvalidMarker:
            return False
    values = []
    for alternative in re.split(r"\s+or\s+", marker):
        clauses = re.split(r"\s+and\s+", alternative)
        results = []
        for clause in clauses:
            m = MARKER_CLAUSE.match(clause)
            if not m:
                return False  # not a marker this reads: don't guess
            actual = env.get(m[1], "")
            results.append(actual == m[4] if m[2] == "==" else actual != m[4])
        values.append(all(results))
    return not any(values)


def declared_license(meta: metadata.PackageMetadata) -> str:
    expression = meta.get("License-Expression")
    if expression:
        return expression
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or [] if c.startswith("License :: ")]
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 80:
        return text if not classifiers else f"{text} ({'; '.join(classifiers)})"
    if classifiers:
        return "; ".join(classifiers)
    return "see the license text below" if text else "UNKNOWN"


def license_texts(dist: metadata.Distribution) -> list[tuple[str, str]]:
    texts = []
    for f in dist.files or []:
        name = str(f).replace("\\", "/")
        if ".dist-info/" not in name or not LICENSE_FILE.search(name):
            continue
        try:
            body = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if body and body.strip():
            texts.append((name.split(".dist-info/", 1)[1], body.strip()))
    if not texts:
        body = (dist.metadata.get("License") or "").strip()
        if "\n" in body:
            texts.append(("License field", body))
    return sorted(texts)


def python_section(name: str, version: str) -> tuple[str, str]:
    dist = metadata.distribution(name)
    meta = dist.metadata
    installed = meta["Version"]
    if installed != version:
        raise SystemExit(f"{name}: uv.lock pins {version} but {installed} is installed")
    source = meta.get("Home-page") or next(
        (u.split(",", 1)[1].strip() for u in meta.get_all("Project-URL") or [] if "," in u), ""
    )
    license_name = declared_license(meta)
    lines = [f"{meta['Name']} {installed}", f"License: {license_name}"]
    if source:
        lines.append(f"Source: {source}")
    texts = license_texts(dist)
    body = "\n".join(lines) + "\n"
    if texts:
        body += "\n" + "\n".join(f"--- {n} ---\n{t}\n" for n, t in texts)
    else:
        body += "(no license file in the package)\n"
    return license_name, body


def build(
    requirements: str, npm: str, python_version: str, env: dict[str, str] | None = None
) -> tuple[str, list[str]]:
    sections, problems = [], []
    for name, version, marker in parse_requirements(requirements):
        try:
            license_name, body = python_section(name, version)
        except metadata.PackageNotFoundError:
            if marker_excludes(marker, env):
                continue  # its environment marker is false here, so it is never installed on this platform
            problems.append(f"{name}=={version} is in uv export but not installed" + (f" (marker: {marker})" if marker else ""))
            continue
        if license_name == "UNKNOWN":
            problems.append(f"{name}: no license declared")
        sections.append(body)
    if not sections:
        problems.append("no Python packages read: is --requirements uv export's output?")
    if not npm.strip():
        problems.append("the npm part is empty")
    out = (
        HEADER.format(python=python_version)
        + f"\n{RULE}\nPART 1. PYTHON PACKAGES ({len(sections)})\n{RULE}\n\n"
        + f"\n{RULE}\n\n".join(sections)
        + f"\n{RULE}\nPART 2. NPM PACKAGES\n{RULE}\n\n"
        + npm.rstrip()
        + "\n"
    )
    return out, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requirements", required=True)
    parser.add_argument("--npm", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    with open(args.requirements, encoding="utf-8") as fh:
        requirements = fh.read()
    with open(args.npm, encoding="utf-8") as fh:
        npm = fh.read()
    version = ".".join(map(str, sys.version_info[:3]))
    text, problems = build(requirements, npm, version)
    for p in problems:
        print(f"third-party notices: {p}", file=sys.stderr)
    # A package with no declared license is listed (and named above) rather than failing the build; a missing one is not.
    if any("not installed" in p or "not read" in p or "empty" in p for p in problems):
        return 1
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"third-party notices: {args.out}, {len(text):,} characters", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
