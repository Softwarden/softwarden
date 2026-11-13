"""The backup's place in the stack (core/backup.py): the worker writes sets into the softwarden-backups volume
as the image user, reads the generated key from the secrets volume, and the image carries the PostgreSQL 17 client and
openssl. ops/restore.sh restores and rehearses sets from either writer."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_compose import REPO, services, volumes


def test_only_the_worker_mounts_the_backup_volume_and_it_is_declared():
    s = services("compose.yaml")
    assert "softwarden-backups:/data/backups" in volumes(s["worker"])
    assert not any("backups" in v for v in volumes(s["app"]))  # the app reads status from the database
    assert "softwarden-secrets:/data/secrets:ro" in volumes(s["worker"])  # the generated key, read-only
    compose = (REPO / "compose.yaml").read_text(encoding="utf-8")
    assert re.search(r"^  softwarden-backups:", compose, re.MULTILINE)


def test_the_backup_settings_reach_app_worker_and_init():
    s = services("compose.yaml")
    for name in ("app", "worker", "init"):
        assert s[name]["environment"]["SOFTWARDEN_BACKUP_KEY_FILE"] == "${SOFTWARDEN_BACKUP_KEY_FILE:-}", name
    for name in ("app", "worker"):
        env = s[name]["environment"]
        assert env["SOFTWARDEN_BACKUP_CRON"] == "${SOFTWARDEN_BACKUP_CRON:-30 2 * * *}"
        # The image sets the destination; compose must not blank it.
        assert "SOFTWARDEN_BACKUP_DIR" not in env


def test_the_image_has_the_tools_and_a_backup_folder_for_its_user():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "SOFTWARDEN_BACKUP_DIR=/data/backups" in dockerfile
    assert "postgresql-client-17" in dockerfile and "bookworm-pgdg" in dockerfile and " openssl" in dockerfile
    assert re.search(r"install -d -m 0700 -o softwarden -g softwarden [^\n]*/data/backups", dockerfile)
    assert 'pg_dump --version | grep -q " 17\\."' in dockerfile  # the build fails on any other major version


PGDG_FINGERPRINT = "B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8"
PGDG_KEY = REPO / "docker" / "pgdg" / "ACCC4CF8.asc"


def _openpgp_packets(data: bytes):
    """(tag, body) of each OpenPGP packet (RFC 4880 section 4.2, old and new formats)."""
    i = 0
    while i < len(data):
        header = data[i]
        assert header & 0x80, "not an OpenPGP packet"
        if header & 0x40:  # new format
            tag, i = header & 0x3F, i + 1
            first = data[i]
            if first < 192:
                length, i = first, i + 1
            elif first < 224:
                length, i = ((first - 192) << 8) + data[i + 1] + 192, i + 2
            else:
                length, i = int.from_bytes(data[i + 1 : i + 5], "big"), i + 5
        else:  # old format
            tag, kind, i = (header >> 2) & 0x0F, header & 0x03, i + 1
            size = {0: 1, 1: 2, 2: 4}[kind]
            length, i = int.from_bytes(data[i : i + size], "big"), i + size
        yield tag, data[i : i + length]
        i += length


def test_the_pgdg_signing_key_is_committed_and_has_the_published_fingerprint():
    """No network trust at build time: the Dockerfile copies the committed key, and its
    primary key's v4 fingerprint (SHA-1 of 0x99, the 2-byte length and the public-key packet) is PGDG's."""
    import base64
    import hashlib

    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY docker/pgdg/ACCC4CF8.asc /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc" in dockerfile
    assert PGDG_FINGERPRINT in dockerfile
    assert "urlretrieve" not in dockerfile and "postgresql.org/media/keys" not in dockerfile
    armored = PGDG_KEY.read_text(encoding="ascii").splitlines()
    begin = armored.index("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    body = armored[begin + 1 : armored.index("-----END PGP PUBLIC KEY BLOCK-----")]
    blank = body.index("")  # armor headers end at the first blank line
    data = base64.b64decode("".join(line for line in body[blank + 1 :] if not line.startswith("=")))
    tag, packet = next(_openpgp_packets(data))
    assert tag == 6 and packet[0] == 4  # a v4 public key comes first
    fingerprint = hashlib.sha1(b"\x99" + len(packet).to_bytes(2, "big") + packet).hexdigest().upper()  # noqa: S324
    assert fingerprint == PGDG_FINGERPRINT


def test_restore_has_a_non_interactive_rehearsal():
    script = (REPO / "ops" / "restore.sh").read_text(encoding="utf-8")
    for flag in ("--rehearse)", "--migrate)"):
        assert flag in script
    for check in ("migrate --check", "upgrade_check --manifest", "verify_attachments"):
        assert check in script
    assert 'DROP DATABASE IF EXISTS \\"$scratch_db\\"' in script  # dropped on exit, pass or fail
    # Never into another existing database (it would be dropped), and never the live media swap while rehearsing.
    assert '[ -n "$exists" ] && [ "$db_name" != "softwarden_rehearsal" ]' in script
    assert 'if [ "$check_media" -eq 1 ] && [ "$rehearse" -eq 0 ] && [ "$db_name" = "softwarden" ]; then' in script
    assert not re.search(r"(^\s*|[;&|]\s*)read[ \t]", script, re.MULTILINE)  # never prompts


@pytest.mark.parametrize("name", ["backup.sh", "restore.sh", "common.sh"])
def test_ops_scripts_parse(name):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash isn't installed here")
    result = subprocess.run([bash, "-n", str(Path(REPO) / "ops" / name)], capture_output=True, text=True)  # noqa: S603
    assert result.returncode == 0, result.stderr
