[CmdletBinding()]
param(
    [int]$BackendPort = $(if ($env:PADOS_API_PORT) { [int]$env:PADOS_API_PORT } else { 8088 }),
    [int]$FrontendPort = 5173,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$WebRoot = Join-Path $Root "web"
$LogRoot = Join-Path $Root "runtime\logs"
$BackendOut = Join-Path $LogRoot "web-backend.stdout.log"
$BackendErr = Join-Path $LogRoot "web-backend.stderr.log"
$FrontendOut = Join-Path $LogRoot "web-frontend.stdout.log"
$FrontendErr = Join-Path $LogRoot "web-frontend.stderr.log"
$BackendProcess = $null
$FrontendProcess = $null

function Find-FreePort([int]$Candidate) {
    for ($port = $Candidate; $port -lt ($Candidate + 50); $port++) {
        $listener = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if (-not $listener) { return $port }
    }
    throw "NO_FREE_LOCAL_PORT_FROM_$Candidate"
}

function Wait-Http([string]$Url, [int]$Attempts = 30) {
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        try {
            $response = Invoke-RestMethod -Uri $Url -Method Get -TimeoutSec 3
            if ($response) { return $response }
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    throw "HTTP_HEALTHCHECK_FAILED_$Url"
}

function Stop-Child([System.Diagnostics.Process]$Process) {
    if ($Process -and -not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        Get-Process -Id $Process.Id -ErrorAction SilentlyContinue | Wait-Process -Timeout 3 -ErrorAction SilentlyContinue
    }
}

function Stop-PortListener([int]$Port) {
    $listeners = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "PADOS_PYTHON_NOT_FOUND_$Python" }
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { throw "NPM_NOT_FOUND" }
if (-not (Test-Path -LiteralPath (Join-Path $WebRoot "node_modules") -PathType Container)) {
    throw "FRONTEND_DEPENDENCIES_NOT_INSTALLED_RUN_NPM_INSTALL_IN_WEB"
}

$BackendPort = Find-FreePort $BackendPort
$FrontendPort = Find-FreePort $FrontendPort
$BackendUrl = "http://127.0.0.1:$BackendPort"
$FrontendUrl = "http://127.0.0.1:$FrontendPort"

try {
    Push-Location $Root
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $doctorOutput = @(& $Python -m pados.cli.main doctor --json 2>&1)
    $doctorExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorActionPreference
    if ($doctorExitCode -ne 0) { throw "PADOS_DOCTOR_FAILED_$doctorExitCode" }

    $BackendProcess = Start-Process -FilePath $Python -ArgumentList @(
        "-m", "uvicorn", "pados.api.app:app", "--host", "127.0.0.1", "--port", "$BackendPort"
    ) -WorkingDirectory $Root -RedirectStandardOutput $BackendOut -RedirectStandardError $BackendErr -WindowStyle Hidden -PassThru
    $null = Wait-Http "$BackendUrl/api/health"

    $frontendCommand = "`$env:PADOS_WEB_BACKEND_URL='$BackendUrl'; Set-Location -LiteralPath '$WebRoot'; & npm.cmd run dev -- --host 127.0.0.1 --port $FrontendPort"
    $FrontendProcess = Start-Process -FilePath "powershell.exe" -ArgumentList @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $frontendCommand
    ) -WorkingDirectory $WebRoot -RedirectStandardOutput $FrontendOut -RedirectStandardError $FrontendErr -WindowStyle Hidden -PassThru

    $frontendReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $FrontendPort -State Listen -ErrorAction SilentlyContinue) {
            $frontendReady = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }
    if (-not $frontendReady) { throw "FRONTEND_START_FAILED" }
    if (-not $NoBrowser) { Start-Process $FrontendUrl | Out-Null }

    Write-Host "PADOS Web Control Center is running."
    Write-Host "Frontend: $FrontendUrl"
    Write-Host "Backend:  $BackendUrl"
    Write-Host "Press Ctrl+C to stop both child processes."
    while ($true) {
        if ($BackendProcess.HasExited) { throw "BACKEND_EXITED_$($BackendProcess.ExitCode)" }
        if ($FrontendProcess.HasExited) { throw "FRONTEND_EXITED_$($FrontendProcess.ExitCode)" }
        Start-Sleep -Seconds 1
    }
} finally {
    Pop-Location
    Stop-Child $FrontendProcess
    Stop-Child $BackendProcess
    Stop-PortListener $FrontendPort
    Stop-PortListener $BackendPort
}
