"""scripts/setup.sh's env_get and setup.ps1's Get-EnvValue read a .env value as Compose does: a
trailing CR from a file saved on Windows, surrounding blanks and one pair of matching quotes are not part of it. The
functions are cut out of the scripts and run on their own, so nothing else in setup runs."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SH = shutil.which("sh") or ""
PWSH = shutil.which("pwsh") or shutil.which("powershell") or ""

ENV = (
    "SOFTWARDEN_HTTPS_PORT=8443\r\n"
    'PUBLIC_HOSTNAME="softwarden.example.com"\r\n'
    "SOFTWARDEN_HTTP_PORT='8080'\r\n"
    "SPACED=  padded  \n"
    'HALF="open\n'
    "EMPTY=\n"
    "LAST=one\n"
    "LAST=two\r\n"
)
WANT = {
    "SOFTWARDEN_HTTPS_PORT": "8443",
    "PUBLIC_HOSTNAME": "softwarden.example.com",
    "SOFTWARDEN_HTTP_PORT": "8080",
    "SPACED": "padded",
    "HALF": '"open',
    "EMPTY": "",
    "LAST": "two",
    "MISSING": "",
}


@pytest.mark.skipif(not SH, reason="needs sh")
def test_setup_sh_env_get(tmp_path):
    script = (REPO / "scripts" / "setup.sh").read_text(encoding="utf-8")
    function = re.search(r"^CR=\$\(printf '\\r'\)\nenv_get\(\) \{\n.*?^\}\n", script, re.MULTILINE | re.DOTALL)
    assert function, "env_get not found in scripts/setup.sh"
    env = tmp_path / ".env"
    env.write_bytes(ENV.encode())
    probe = tmp_path / "probe.sh"
    probe.write_text(
        f'ENV_FILE="$1"\n{function.group(0)}' + "".join(f'printf "%s=[%s]\\n" {k} "$(env_get {k})"\n' for k in WANT),
        encoding="utf-8",
        newline="\n",
    )
    out = subprocess.run(  # noqa: S603
        [SH, probe.as_posix(), env.as_posix()], capture_output=True, check=True
    ).stdout.decode()
    assert out.splitlines() == [f"{k}=[{v}]" for k, v in WANT.items()]


@pytest.mark.skipif(not PWSH, reason="needs PowerShell")
def test_setup_ps1_get_env_value(tmp_path):
    script = (REPO / "scripts" / "setup.ps1").read_text(encoding="utf-8")
    function = re.search(r"^function Get-EnvValue\(.*?^\}\r?\n", script, re.MULTILINE | re.DOTALL)
    assert function, "Get-EnvValue not found in scripts/setup.ps1"
    env = tmp_path / ".env"
    env.write_bytes(ENV.encode())
    probe = tmp_path / "probe.ps1"
    probe.write_text(
        f"$EnvFile = '{env}'\n{function.group(0)}"
        + "".join(f"Write-Output \"{k}=[$(Get-EnvValue '{k}')]\"\n" for k in WANT),
        encoding="utf-8",
    )
    out = subprocess.run(  # noqa: S603
        [PWSH, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(probe)],
        capture_output=True,
        check=True,
    ).stdout.decode()
    assert out.splitlines() == [f"{k}=[{v}]" for k, v in WANT.items()]
