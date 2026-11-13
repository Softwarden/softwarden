#Requires -Version 5.1
<#
.SYNOPSIS
  First-run setup (and safe re-run) for Softwarden on Windows: one command from a fresh clone to the first-run page.

.DESCRIPTION
  Run from anywhere, in Windows PowerShell 5.1 or PowerShell 7:
    powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
    pwsh scripts/setup.ps1 -TrustLocalCA

  1. Checks Docker and Compose v2, TLS interception (exporting the network's root from the Windows store into
     ca-certs/ if you agree), and that the HTTP/HTTPS ports are free.
  2. Builds and starts the stack and waits until it is healthy. The containers generate their own secrets on first
     start (compose.yaml's init service) and the one-shot migrate service applies migrations before the app starts.
  3. Runs `manage.py doctor` (the installation's checks, no secrets).
  4. On an interactive console, runs bootstrap_admin: the one-time setup token while setup is open, and the first
     time the credential encryption key to back up. Nothing secret is printed on a non-interactive run.

  Optional: `docker compose up -d --build` does step 2 on its own. Safe to re-run. scripts/setup.sh does the same on
  Linux and macOS.

.PARAMETER TrustLocalCA
  Also offer to add Caddy's local root CA to your user's Trusted Root store, so browsers stop warning. Says what it
  will do and asks first; Windows shows its own confirmation too. Never done without this switch.
#>
[CmdletBinding()]
param(
    [switch]$TrustLocalCA
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$EnvFile = Join-Path $Root ".env"
$CheckImage = "python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72"  # the base the build uses: it trusts what the build trusts
# No build provenance attestation: it carries a timestamp, so each rebuild would get a new image digest and compose
# would recreate containers even when nothing changed (re-runs must not).
$env:BUILDX_NO_DEFAULT_ATTESTATIONS = "1"

function Step([string]$Text) { Write-Host "`n== $Text" -ForegroundColor Cyan }
function Info([string]$Text) { Write-Host "   $Text" }
function Fail([string]$Text) {
    Write-Host "`nSetup stopped: $Text" -ForegroundColor Red
    exit 1
}
function Invoke-Quiet([string[]]$Arguments) {
    # Runs docker with these arguments and returns its output; $LASTEXITCODE is left set. Windows PowerShell 5.1 turns
    # a native command's stderr into errors, which "Stop" would make fatal, so this relaxes it for the call.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $output = & docker @Arguments 2>&1 | ForEach-Object { "$_" } }
    finally { $ErrorActionPreference = $previous }
    return $output
}
function Test-Interactive {
    return (-not [Console]::IsInputRedirected) -and (-not [Console]::IsOutputRedirected)
}

# --- settings: the environment first, then .env, then the default ----------------------------------------------

function Get-EnvValue([string]$Key) {
    $value = ""
    if (Test-Path $EnvFile) {
        foreach ($line in ([IO.File]::ReadAllText($EnvFile) -split "`r?`n")) {
            if ($line -match "^\s*$([regex]::Escape($Key))\s*=(.*)$") { $value = $Matches[1].Trim() }
        }
    }
    # One pair of matching quotes around the value, as Compose reads .env (and scripts/setup.sh's env_get).
    if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    return $value
}

function Get-Setting([string]$Key, [string]$Default) {
    $fromShell = [Environment]::GetEnvironmentVariable($Key)
    if ($fromShell) { return $fromShell }
    $fromFile = Get-EnvValue $Key
    if ($fromFile) { return $fromFile }
    return $Default
}

# --- preflight ---------------------------------------------------------------------------------------------------

Step "Checking Docker"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker isn't installed or isn't on PATH. Install Docker Desktop, start it, and run this again."
}
$composeVersion = Invoke-Quiet @("compose", "version", "--short")
if ($LASTEXITCODE -ne 0 -or -not ("$composeVersion" -match "^v?2\.")) {
    Fail "Docker Compose v2 is required ('docker compose version' should report 2.x)."
}
$null = Invoke-Quiet @("info", "--format", "{{.ServerVersion}}")
if ($LASTEXITCODE -ne 0) { Fail "The Docker engine isn't running. Start Docker Desktop and run this again." }
Info "Docker Compose $composeVersion"

# --- TLS interception --------------------------------------------------------------------------------------------

$TlsCheck = @'
import glob, socket, ssl, sys
host = sys.argv[1]
for path in glob.glob("/extra/*.crt"):  # every file must be a usable certificate
    try:
        ssl.create_default_context().load_verify_locations(path)
    except (ssl.SSLError, OSError):
        print("badfile", path.rsplit("/", 1)[-1]); sys.exit()
def connect(extra):
    ctx = ssl.create_default_context()
    if extra:
        for path in glob.glob("/extra/*.crt"):
            ctx.load_verify_locations(path)
    with socket.create_connection((host, 443), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=host):
            pass
try:
    connect(False); print("ok")
except ssl.SSLCertVerificationError:
    try:
        connect(True); print("ok-extra")
    except ssl.SSLCertVerificationError as exc:
        print("intercepted", exc.verify_message)
    except OSError as exc:
        print("unreachable", exc)
except OSError as exc:
    print("unreachable", exc)
'@

function Test-Tls([string]$TlsHost) {
    $certs = Join-Path $Root "ca-certs"
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $out = $TlsCheck | & docker run --rm -i -v "$($certs):/extra:ro" $CheckImage python - $TlsHost 2>&1 }
    finally { $ErrorActionPreference = $previous }
    $lines = @($out | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
    if ($lines.Count -eq 0) { return "unreachable (no output from the check)" }
    return $lines[-1]
}

function Get-WindowsRoot([string]$TlsHost) {
    # The chain Windows builds for the host, read only to find its root; nothing is sent.
    $client = New-Object Net.Sockets.TcpClient($TlsHost, 443)
    try {
        $callback = [Net.Security.RemoteCertificateValidationCallback] { param($s, $c, $ch, $e) $true }
        $stream = New-Object Net.Security.SslStream($client.GetStream(), $false, $callback)
        $stream.AuthenticateAsClient($TlsHost)
        $leaf = New-Object Security.Cryptography.X509Certificates.X509Certificate2($stream.RemoteCertificate)
        $chain = New-Object Security.Cryptography.X509Certificates.X509Chain
        $trusted = $chain.Build($leaf)
        $rootCert = $chain.ChainElements[$chain.ChainElements.Count - 1].Certificate
        return @{ Trusted = $trusted; Root = $rootCert }
    }
    finally { $client.Close() }
}

function ConvertTo-Pem($Certificate) {
    $base64 = [Convert]::ToBase64String($Certificate.RawData, [Base64FormattingOptions]::InsertLineBreaks)
    return "-----BEGIN CERTIFICATE-----`n" + $base64.Replace("`r", "") + "`n-----END CERTIFICATE-----`n"
}

Step "Checking TLS (can the build download what it needs?)"
$TlsHost = if ($env:SOFTWARDEN_TLS_CHECK_HOST) { $env:SOFTWARDEN_TLS_CHECK_HOST } else { "github.com" }
$null = Invoke-Quiet @("pull", "-q", $CheckImage)
if ($LASTEXITCODE -ne 0) { Fail "Couldn't pull $CheckImage. Check this machine's internet access." }
$tls = Test-Tls $TlsHost
if ($tls -eq "ok") { Info "Public certificates: no interception." }
elseif ($tls -eq "ok-extra") { Info "TLS is inspected on this network; the root in ca-certs/ is trusted." }
elseif ($tls -like "badfile*") {
    Fail "ca-certs/$($tls.Substring(8)) isn't a PEM certificate. Replace it with the root CA as PEM (text starting -----BEGIN CERTIFICATE-----); see ca-certs/README.md."
}
elseif ($tls -like "intercepted*") {
    Info "This network inspects TLS: $TlsHost doesn't present a public certificate ($($tls.Substring(12)))."
    $found = $null
    try { $found = Get-WindowsRoot $TlsHost } catch { $found = $null }
    if ($found -and $found.Trusted) {
        $subject = $found.Root.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
        $file = Join-Path $Root ("ca-certs/" + (($subject -replace "[^A-Za-z0-9._-]+", "-").Trim("-")) + ".crt")
        Info "Windows trusts it through the root '$subject' (installed by your IT team)."
        $answer = "n"
        if (Test-Interactive) { $answer = Read-Host "   Export that root to $file so the build trusts it too? [Y/n]" }
        if ($answer -eq "" -or $answer -match "^[Yy]") {
            [IO.File]::WriteAllText($file, (ConvertTo-Pem $found.Root), (New-Object Text.UTF8Encoding($false)))
            $tls = Test-Tls $TlsHost
            if ($tls -ne "ok-extra" -and $tls -ne "ok") { Fail "Exported '$subject', but the check still fails ($tls). Ask IT for the root CA your proxy uses and put it in ca-certs/ (see ca-certs/README.md)." }
            Info "Exported. The build will trust it."
        }
        else {
            Fail "Put the network's root CA in ca-certs/ as a PEM .crt (see ca-certs/README.md), or re-run this interactively to export '$subject'."
        }
    }
    else {
        Fail "Windows doesn't trust this certificate either, so it can't be exported safely. Ask IT for the root CA your proxy uses, save it as PEM in ca-certs/<name>.crt (see ca-certs/README.md), and run this again."
    }
}
else { Fail "Couldn't reach $TlsHost over HTTPS ($tls). Check this machine's internet access." }

# --- ports -------------------------------------------------------------------------------------------------------

$HttpPort = Get-Setting "HTTP_PORT" "80"
$HttpsPort = Get-Setting "HTTPS_PORT" "443"
Step "Checking ports"
$proxyRunning = Invoke-Quiet @("compose", "ps", "-q", "proxy")
if ($LASTEXITCODE -eq 0 -and ("$proxyRunning".Trim() -match "^[0-9a-f]{12,}")) { Info "Softwarden's proxy is already running on its ports." }
else {
    foreach ($pair in @(@("HTTP_PORT", $HttpPort), @("HTTPS_PORT", $HttpsPort))) {
        $name = $pair[0]
        $port = $pair[1]
        $listening = Get-NetTCPConnection -State Listen -LocalPort ([int]$port) -ErrorAction SilentlyContinue
        if ($listening) {
            $owner = (Get-Process -Id (@($listening)[0]).OwningProcess -ErrorAction SilentlyContinue).ProcessName
            $by = if ($owner) { " by $owner" } else { "" }
            Fail "Port $port is in use$by. Stop it, or set $name in .env to a free port (e.g. 8443) and run this again."
        }
    }
    Info "Ports $HttpPort and $HttpsPort are free."
}

# --- start -------------------------------------------------------------------------------------------------------

Step "Building and starting Softwarden (the first build takes a few minutes)"
# `--wait` waits for every healthcheck, the worker's heartbeat included (compose.yaml); the exit code is what counts.
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try { & docker compose up -d --build --wait --wait-timeout 600 }
finally { $ErrorActionPreference = $previous }
if ($LASTEXITCODE -ne 0) {
    & docker compose ps
    & docker compose logs --tail 40 init app
    Fail "Softwarden didn't become healthy. The output above shows why."
}

$publicHost = Get-Setting "PUBLIC_HOSTNAME" "localhost"
$url = "https://$publicHost"
if ($HttpsPort -ne "443") { $url = "$($url):$HttpsPort" }

$facts = Invoke-Quiet @("compose", "exec", "-T", "app", "python", "manage.py", "setup_status")
# The whole setup at a glance (no secrets): a FAIL line says what to fix; warnings about first-run setup and the first
# backup are expected on a new install (docs/INSTALL.md, step 4c).
Step "Checking the installation (manage.py doctor)"
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try { & docker compose exec -T app python manage.py doctor }
finally { $ErrorActionPreference = $previous }
if ($LASTEXITCODE -ne 0) { Info "doctor found a failure above; its fix line says what to do." }
Step "Ready"
if ("$facts" -match "setup_pending=1") {
    Info "Open $url, accept the certificate warning (Caddy's local CA), and enter the one-time token below."
}
else { Info "Softwarden is running at $url. First-run setup is already done; sign in." }
if (Test-Interactive) {
    # The token while setup is open and, the first time only, the credential key to back up.
    & docker compose exec app python manage.py bootstrap_admin
}
else {
    Info "To see the setup token and, the first time, the credential key, run in an interactive console:"
    Info "  docker compose exec app python manage.py bootstrap_admin"
}
if ($HttpsPort -ne "443") {
    Info "(The address it prints assumes port 443; use $url, or set SOFTWARDEN_BASE_URL=$url in .env.)"
}

Write-Host ""
Write-Host "   Back up the credential encryption key off this machine (a password manager). Database backups don't" -ForegroundColor Yellow
Write-Host "   include it, and without it no secret saved in Softwarden can be read. 'docker compose down -v' deletes it." -ForegroundColor Yellow

# --- optional: trust Caddy's local root CA for this Windows user ---------------------------------------------------

if ($TrustLocalCA) {
    Step "Trusting Caddy's local root CA (optional)"
    $certDir = Join-Path $Root "certs"
    if (-not (Test-Path $certDir)) { $null = New-Item -ItemType Directory -Path $certDir }
    $caFile = Join-Path $certDir "softwarden-local-root.crt"
    $null = Invoke-Quiet @("compose", "cp", "proxy:/data/caddy/pki/authorities/local/root.crt", $caFile)
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $caFile)) { Fail "Couldn't copy Caddy's root certificate (is CADDY_TLS 'internal'?)." }
    $cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2($caFile)
    Info "This will add this certificate to your user's Trusted Root Certification Authorities store:"
    Info "  Subject:    $($cert.Subject)"
    Info "  Thumbprint: $($cert.Thumbprint)"
    Info "  Valid to:   $($cert.NotAfter)"
    Info "Browsers using the Windows store (Edge, Chrome) will then trust every certificate Caddy's local CA issues."
    Info "Undo: certmgr.msc > Trusted Root Certification Authorities > Certificates, delete it."
    if (-not (Test-Interactive)) { Fail "Not an interactive console; nothing was changed." }
    $answer = Read-Host "   Go ahead? [y/N]"
    if ($answer -match "^[Yy]") {
        $store = New-Object Security.Cryptography.X509Certificates.X509Store("Root", "CurrentUser")
        $store.Open("ReadWrite")
        try { $store.Add($cert) } finally { $store.Close() }
        Info "Trusted. Restart the browser."
    }
    else { Info "Nothing was changed." }
}
