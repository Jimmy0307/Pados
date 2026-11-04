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

function Get-StartRefusal([string]$ErrorLog) {
    # `pados serve` prints its refusal as "Domain error: <CODE>: <next step>" on standard error.
    if (-not (Test-Path -LiteralPath $ErrorLog -PathType Leaf)) { return "no output" }
    $lines = @(Get-Content -LiteralPath $ErrorLog -Encoding UTF8 -ErrorAction SilentlyContinue)
    $refusal = $lines | Where-Object { $_ -match "error:" } | Select-Object -Last 1
    if ($refusal) { return $refusal.Trim() }
    $last = $lines | Where-Object { $_.Trim() } | Select-Object -Last 1
    if ($last) { return $last.Trim() }
    return "no output"
}

function Wait-Backend([string]$Url, [System.Diagnostics.Process]$Process, [string]$ErrorLog, [int]$Attempts = 60) {
    for ($attempt = 0; $attempt -lt $Attempts; $attempt++) {
        if ($Process.HasExited) {
            throw "BACKEND_START_REFUSED (exit $($Process.ExitCode)): $(Get-StartRefusal $ErrorLog)"
        }
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

function Start-PadosBackend([string]$Python, [string]$Root, [int]$Port, [string]$OutLog, [string]$ErrorLog) {
    # The documented start, `pados serve`, with its own checks: it refuses a non-loopback host and the shipped default
    # control token before it listens (no second check lives here). The chosen port reaches it as PADOS_API_PORT.
    $previousPort = $env:PADOS_API_PORT
    $env:PADOS_API_PORT = "$Port"
    try {
        $process = Start-Process -FilePath $Python -ArgumentList @("-m", "pados.cli.main", "serve") `
            -WorkingDirectory $Root -RedirectStandardOutput $OutLog -RedirectStandardError $ErrorLog -WindowStyle Hidden -PassThru
    } finally {
        if ($null -eq $previousPort) { Remove-Item Env:PADOS_API_PORT -ErrorAction SilentlyContinue }
        else { $env:PADOS_API_PORT = $previousPort }
    }
    try {
        $null = Wait-Backend "http://127.0.0.1:$Port/health/live" $process $ErrorLog
    } catch {
        Stop-Child $process
        throw
    }
    return $process
}

function Stop-PortListener([int]$Port) {
    $listeners = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        Stop-Process -Id $listener.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

# Dot-sourcing (the Pester tests) defines the functions only.
if ($MyInvocation.InvocationName -eq ".") { return }

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

    $BackendProcess = Start-PadosBackend $Python $Root $BackendPort $BackendOut $BackendErr

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
    # The root page is the Remote console sign-in; the local operator console is /operator.html.
    if (-not $NoBrowser) { Start-Process "$FrontendUrl/operator.html" | Out-Null }

    Write-Host "PADOS Web Control Center is running."
    Write-Host "Frontend: $FrontendUrl/operator.html (local console)"
    Write-Host "Backend:  $BackendUrl (pados serve)"
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
