#Requires -Version 5.1
<#
.SYNOPSIS
    O.D.I.N. Windows Installer
.DESCRIPTION
    Installs O.D.I.N. print farm management on Windows using Docker Desktop.
    Mirrors the behavior of install.sh for Linux/Mac.
.NOTES
    Requires: Docker Desktop for Windows, WSL2 (for Windows Home)
    Version: matches O.D.I.N. version at time of release
.LINK
    https://runsodin.com
#>

$ErrorActionPreference = "Stop"

$ODIN_VERSION = "1.3.70"
$ODIN_IMAGE = "ghcr.io/hughkantsime/odin:latest"
$ODIN_REPO = "https://raw.githubusercontent.com/HughKantsime/runsodin/master"

# ── Display Helpers ──────────────────────────────────────────────────────────

function Write-Ok    { param([string]$Msg) Write-Host "  ✓ $Msg" -ForegroundColor Green }
function Write-Err   { param([string]$Msg) Write-Host "  ✗ $Msg" -ForegroundColor Red }
function Write-Warn  { param([string]$Msg) Write-Host "  ! $Msg" -ForegroundColor Yellow }
function Write-Dim   { param([string]$Msg) Write-Host "    $Msg" -ForegroundColor DarkGray }
function Write-Phase {
    param([int]$Num, [int]$Total, [string]$Name)
    Write-Host ""
    Write-Host "[$Num/$Total] $Name" -ForegroundColor White -NoNewline
    Write-Host ""
}

function Write-Banner {
    Write-Host @"

     ██████╗ ██████╗ ██╗███╗   ██╗
    ██╔═══██╗██╔══██╗██║████╗  ██║
    ██║   ██║██║  ██║██║██╔██╗ ██║
    ██║   ██║██║  ██║██║██║╚██╗██║
    ╚██████╔╝██████╔╝██║██║ ╚████║
     ╚═════╝ ╚═════╝ ╚═╝╚═╝  ╚═══╝

"@ -ForegroundColor Cyan
    Write-Host "    v$ODIN_VERSION — Orchestrated Dispatch & Inventory Network" -ForegroundColor DarkGray
    Write-Host "    3D Print Farm Management" -ForegroundColor DarkGray
}

function Stop-WithError {
    param([string]$Msg, [string]$Hint = "", [string]$Hint2 = "")
    Write-Err $Msg
    if ($Hint)  { Write-Dim $Hint }
    if ($Hint2) { Write-Dim $Hint2 }
    Write-Host ""
    Write-Host "If the problem persists, open an issue at:" -ForegroundColor DarkGray
    Write-Host "https://github.com/HughKantsime/runsodin/issues" -ForegroundColor DarkGray
    exit 1
}

# ── Utility Functions ────────────────────────────────────────────────────────

function Get-RandomHex {
    param([int]$Bytes = 32)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $buf = New-Object byte[] $Bytes
    $rng.GetBytes($buf)
    return ($buf | ForEach-Object { $_.ToString("x2") }) -join ''
}

function Get-FernetKey {
    # Generate a Fernet-compatible key (32 bytes, base64url-encoded)
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $buf = New-Object byte[] 32
    $rng.GetBytes($buf)
    return [Convert]::ToBase64String($buf).Replace('+', '-').Replace('/', '_')
}

function Get-HostIP {
    try {
        $ip = (Get-NetIPAddress -AddressFamily IPv4 |
               Where-Object { $_.InterfaceAlias -notmatch 'Loopback' -and $_.IPAddress -ne '127.0.0.1' } |
               Select-Object -First 1).IPAddress
        if ($ip) { return $ip }
    } catch {}
    return "localhost"
}

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ── Main ─────────────────────────────────────────────────────────────────────

$StartTime = Get-Date
Write-Banner

$TOTAL = 9

# ── Phase 1: Preflight ──────────────────────────────────────────────────────

Write-Phase 1 $TOTAL "Preflight checks"

# Admin check
if (Test-IsAdmin) {
    Write-Ok "Running as Administrator"
} else {
    Write-Warn "Not running as Administrator — data directory will be in $env:USERPROFILE\odin"
}

# Windows version
$os = [System.Environment]::OSVersion
if ($os.Version.Major -lt 10) {
    Stop-WithError "Windows 10 or later is required" `
        "Current: Windows $($os.Version)"
}
$build = $os.Version.Build
if ($build -lt 18362) {
    Write-Warn "Windows 10 1903+ recommended (build 18362+). Current build: $build"
} else {
    Write-Ok "Windows $($os.Version) (build $build)"
}

# Docker
$dockerExe = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerExe) {
    Stop-WithError "Docker not found" `
        "Install Docker Desktop: https://www.docker.com/products/docker-desktop/" `
        "After installing, restart this script."
}

try {
    $dockerVersion = docker version --format '{{.Server.Version}}' 2>$null
    if (-not $dockerVersion) { throw "empty" }
    Write-Ok "Docker $dockerVersion"
} catch {
    Stop-WithError "Docker daemon not running" `
        "Start Docker Desktop and wait for it to be ready, then re-run this script."
}

# Docker Compose
try {
    $composeVersion = docker compose version --short 2>$null
    if (-not $composeVersion) { throw "empty" }
    Write-Ok "Docker Compose $composeVersion"
} catch {
    Stop-WithError "Docker Compose not found" `
        "Docker Compose is included with Docker Desktop." `
        "Ensure Docker Desktop is installed and updated."
}

# WSL2 check (required for Docker Desktop on Windows Home)
try {
    $wslStatus = wsl --status 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "WSL2 available"
    } else {
        Write-Warn "WSL2 may not be enabled — Docker Desktop requires it on Windows Home"
        Write-Dim "Enable WSL2: wsl --install"
    }
} catch {
    Write-Warn "Could not check WSL2 status"
}

# Disk space
$drive = (Get-Item $PWD).PSDrive
$freeGB = [math]::Round($drive.Free / 1GB, 1)
if ($freeGB -lt 5) {
    Write-Warn "Disk space: $freeGB GB free (5 GB recommended)"
} else {
    Write-Ok "Disk space: $freeGB GB free"
}

# Port check
$portFail = $false
foreach ($port in @(8000, 1984, 8555)) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        Write-Err "Port $port in use"
        $portFail = $true
    }
}
if ($portFail) {
    Stop-WithError "Required ports are in use" `
        "Free ports 8000, 1984, and 8555 and re-run the installer."
}
Write-Ok "Ports 8000, 1984, 8555 free"

# Existing install check
$InstallDir = if (Test-IsAdmin) { "C:\odin" } else { Join-Path $env:USERPROFILE "odin" }
$composePath = Join-Path $InstallDir "docker-compose.yml"

if (Test-Path $composePath) {
    Write-Warn "Existing installation found at $InstallDir"
    $answer = Read-Host "  Reinstall? Existing data will be preserved [y/N]"
    if ($answer -notmatch '^[yY]') {
        Write-Host ""
        Write-Host "  Use 'docker compose pull && docker compose up -d' in $InstallDir to update." -ForegroundColor White
        Write-Host ""
        exit 0
    }
    Write-Ok "Reinstalling..."
}

# ── Phase 2: Configuration ──────────────────────────────────────────────────

Write-Phase 2 $TOTAL "Configuration"

$HostIP = Get-HostIP
Write-Host "  Host IP for camera streaming [$HostIP]: " -ForegroundColor Cyan -NoNewline
$inputIP = Read-Host
if ($inputIP) { $HostIP = $inputIP }
Write-Ok "Host IP: $HostIP"

# Timezone
try {
    $tz = (Get-TimeZone).Id
} catch {
    $tz = "America/New_York"
}
Write-Ok "Timezone: $tz"

# ── Phase 3: Install directory ───────────────────────────────────────────────

Write-Phase 3 $TOTAL "Creating install directory"

if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}
Write-Ok "Created $InstallDir"

# ── Phase 4: Download configuration ─────────────────────────────────────────

Write-Phase 4 $TOTAL "Downloading configuration"

$composeUrl = "$ODIN_REPO/install/docker-compose.yml"
try {
    Write-Host "  Downloading docker-compose.yml..." -ForegroundColor DarkGray -NoNewline
    Invoke-WebRequest -Uri $composeUrl -OutFile $composePath -UseBasicParsing
    Write-Host ""
    Write-Ok "docker-compose.yml"
} catch {
    Write-Host ""
    Stop-WithError "Failed to download docker-compose.yml" `
        "Check your internet connection and try again." `
        "URL: $composeUrl"
}

# Adjust volume mount path for Windows
$composeContent = Get-Content $composePath -Raw
$composeContent = $composeContent -replace '\./odin-data:/data', "$InstallDir\odin-data:/data"
Set-Content -Path $composePath -Value $composeContent -NoNewline

# ── Phase 5: Generate environment ────────────────────────────────────────────

Write-Phase 5 $TOTAL "Generating environment"

$envPath = Join-Path $InstallDir ".env"
$envContent = @"
# O.D.I.N. Environment — generated by Windows installer
ODIN_HOST_IP=$HostIP
TZ=$tz
CORS_ORIGINS=http://${HostIP}:8000,http://localhost:8000,http://localhost:3000
"@

Set-Content -Path $envPath -Value $envContent -NoNewline
Write-Ok ".env written"

# ── Phase 6: Pull image ─────────────────────────────────────────────────────

Write-Phase 6 $TOTAL "Pulling Docker image"

Write-Host "  Pulling $ODIN_IMAGE..." -ForegroundColor DarkGray
try {
    docker pull $ODIN_IMAGE 2>&1 | Out-Null
    Write-Ok "Pulled $ODIN_IMAGE"
} catch {
    Stop-WithError "Failed to pull $ODIN_IMAGE" `
        "Check your internet connection and Docker Hub access." `
        "Try manually: docker pull $ODIN_IMAGE"
}

# ── Phase 7: Start container ────────────────────────────────────────────────

Write-Phase 7 $TOTAL "Starting O.D.I.N."

try {
    $output = docker compose -f $composePath --env-file $envPath up -d 2>&1
    Write-Ok "Container started"
} catch {
    Stop-WithError "Failed to start container" `
        "$output" `
        "Check: docker compose -f $composePath logs"
}

# ── Phase 8: Health check ───────────────────────────────────────────────────

Write-Phase 8 $TOTAL "Waiting for healthy"

$attempts = 0
$maxAttempts = 60
$healthy = $false

while ($attempts -lt $maxAttempts) {
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:8000/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($resp.StatusCode -eq 200) {
            $healthy = $true
            break
        }
    } catch {}

    $attempts++
    if ($attempts % 10 -eq 0) {
        Write-Host "  Still waiting... ($attempts/$maxAttempts)" -ForegroundColor DarkGray
    }
    Start-Sleep -Seconds 1
}

if ($healthy) {
    Write-Ok "O.D.I.N. is healthy"
    Write-Ok "API responding on port 8000"
} else {
    Write-Warn "Container did not respond within ${maxAttempts}s"
    Write-Dim "Check logs: docker compose -f $composePath logs"
    Write-Dim "The container may still be starting — wait and check: docker ps"
}

# ── Phase 9: Complete ────────────────────────────────────────────────────────

Write-Phase 9 $TOTAL "Complete!"

$elapsed = [math]::Round(((Get-Date) - $StartTime).TotalSeconds)

Write-Host ""
Write-Host "  ╔══════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "  ║  O.D.I.N. is ready!                      ║" -ForegroundColor Cyan
Write-Host "  ╠══════════════════════════════════════════╣" -ForegroundColor Cyan
Write-Host "  ║                                          ║" -ForegroundColor Cyan
Write-Host "  ║  URL     http://${HostIP}:8000" -ForegroundColor Cyan -NoNewline
Write-Host "$(' ' * [math]::Max(0, 28 - $HostIP.Length))║" -ForegroundColor Cyan
Write-Host "  ║  Setup   Create admin account in browser ║" -ForegroundColor Cyan
Write-Host "  ║  Data    $InstallDir\odin-data\" -ForegroundColor Cyan -NoNewline
Write-Host "$(' ' * [math]::Max(0, 28 - $InstallDir.Length))║" -ForegroundColor Cyan
Write-Host "  ║  Logs    docker compose logs -f          ║" -ForegroundColor Cyan
Write-Host "  ║  Update  docker compose pull && up -d    ║" -ForegroundColor Cyan
Write-Host "  ║                                          ║" -ForegroundColor Cyan
Write-Host "  ╚══════════════════════════════════════════╝" -ForegroundColor Cyan

Write-Host ""
Write-Host "  Installed in $elapsed seconds." -ForegroundColor DarkGray

# Firewall reminder
Write-Host ""
Write-Warn "Windows Firewall may block port 8000. If you can't access ODIN from other devices:"
Write-Dim "Run as Administrator: netsh advfirewall firewall add rule name=`"ODIN`" dir=in action=allow protocol=tcp localport=8000"
Write-Host ""

# Open browser
try {
    Start-Process "http://localhost:8000"
} catch {}
