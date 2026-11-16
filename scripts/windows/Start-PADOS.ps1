# SPDX-License-Identifier: AGPL-3.0-only
<#
.SYNOPSIS
    Start, stop or inspect the local PADOS stack (pados serve + the web Control Center) from a checkout.

.DESCRIPTION
    Windows PowerShell 5.1 compatible. Run from anywhere inside a PADOS checkout (or pass -Root). The launcher:
      1. finds the checkout root (pyproject.toml + src\pados);
      2. checks .venv\Scripts\python.exe, npm.cmd, web\node_modules and .env, printing the exact one-line fix for
         anything missing;
      3. reads only non-secret settings from .env (database host/port, API host, identity flag, allowed origins);
         it never prints a value from .env other than those, and never writes .env;
      4. checks that PostgreSQL accepts TCP connections and that the API and web ports are free;
      5. starts `pados serve` and the Vite dev server as child processes, logs under runtime\logs\, PIDs in
         runtime\local\pados-launcher.json;
      6. waits for /health/ready, opens http://127.0.0.1:<WebPort>/operator.html, and prints how to pair the
         browser when identity sessions are enabled.
    It never runs `pados init`, never edits .env, and never touches a database.

.EXAMPLE
    .\scripts\windows\Start-PADOS.ps1                  # start on 8088 (API) and 5173 (web)
    .\scripts\windows\Start-PADOS.ps1 -DryRun          # print the checks and the plan; start nothing
    .\scripts\windows\Start-PADOS.ps1 -Status          # what is running
    .\scripts\windows\Start-PADOS.ps1 -Stop            # stop what this launcher started
#>
[CmdletBinding(DefaultParameterSetName = "Start")]
param(
    [Parameter(ParameterSetName = "Start")][ValidateRange(1024, 65535)][int]$ApiPort = 8088,
    [Parameter(ParameterSetName = "Start")][ValidateRange(1024, 65535)][int]$WebPort = 5173,
    [Parameter(ParameterSetName = "Start")][switch]$DryRun,
    [Parameter(ParameterSetName = "Start")][switch]$NoBrowser,
    [Parameter(ParameterSetName = "Start")][ValidateRange(5, 600)][int]$ReadyTimeoutSeconds = 90,
    [Parameter(ParameterSetName = "Stop", Mandatory = $true)][switch]$Stop,
    [Parameter(ParameterSetName = "Status", Mandatory = $true)][switch]$Status,
    [string]$Root
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$PidFileName = "pados-launcher.json"
$PidSchema = "pados.launcher.pids.v1"
$DefaultOrigins = "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:8088,http://localhost:8088"
# The only .env keys this launcher reads. Everything else in .env (passwords, token hashes) is never read.
$EnvKeys = @("PADOS_DB_HOST", "PADOS_DB_PORT", "PADOS_API_HOST", "PADOS_IDENTITY_SESSIONS_ENABLED",
    "PADOS_IDENTITY_ALLOWED_ORIGINS")
$LoopbackHosts = @("127.0.0.1", "localhost", "::1")

function Write-Line([string]$Tag, [string]$Text) { Write-Host ("[{0}] {1}" -f $Tag, $Text) }

function Find-PadosRoot([string]$Start) {
    $dir = $Start
    for ($depth = 0; $depth -lt 8 -and $dir; $depth++) {
        if ((Test-Path -LiteralPath (Join-Path $dir "pyproject.toml") -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $dir "src\pados\__init__.py") -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $dir).Path
        }
        $dir = Split-Path -Parent $dir
    }
    return $null
}

function Read-DotEnvSubset([string]$Path, [string[]]$Keys) {
    # Returns only the allowlisted keys. Lines are parsed as KEY=VALUE; quotes and trailing comments are removed.
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $values }
    foreach ($line in (Get-Content -LiteralPath $Path -Encoding UTF8)) {
        if ($line -notmatch '^\s*([A-Z0-9_]+)\s*=\s*(.*)$') { continue }
        $key = $Matches[1]
        if ($Keys -notcontains $key) { continue }
        $value = ($Matches[2] -replace '\s+#.*$', '').Trim().Trim('"').Trim("'")
        $values[$key] = $value
    }
    return $values
}

function Get-EffectiveSetting([hashtable]$DotEnv, [string]$Key, [string]$Default) {
    # Same precedence as pados (pydantic-settings): the process environment wins over .env.
    $fromProcess = [Environment]::GetEnvironmentVariable($Key, "Process")
    if ($fromProcess) { return $fromProcess }
    if ($DotEnv.ContainsKey($Key) -and $DotEnv[$Key]) { return $DotEnv[$Key] }
    return $Default
}

function Get-LauncherPaths([string]$RootPath) {
    $logs = Join-Path $RootPath "runtime\logs"
    return [pscustomobject]@{
        Root        = $RootPath
        Python      = Join-Path $RootPath ".venv\Scripts\python.exe"
        PadosExe    = Join-Path $RootPath ".venv\Scripts\pados.exe"
        Web         = Join-Path $RootPath "web"
        NodeModules = Join-Path $RootPath "web\node_modules"
        DotEnv      = Join-Path $RootPath ".env"
        Logs        = $logs
        LocalState  = Join-Path $RootPath "runtime\local"
        PidFile     = Join-Path $RootPath ("runtime\local\" + $PidFileName)
        ApiOut      = Join-Path $logs "pados-serve.out.log"
        ApiErr      = Join-Path $logs "pados-serve.err.log"
        WebOut      = Join-Path $logs "pados-web.out.log"
        WebErr      = Join-Path $logs "pados-web.err.log"
    }
}

function Get-PortOwner([int]$Port) {
    # Returns $null when nothing listens on the port (any local address), else a short owner description.
    $connections = $null
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        $connections = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    }
    if ($connections -and $connections.Count -gt 0) {
        $owner = $connections[0].OwningProcess
        $name = (Get-Process -Id $owner -ErrorAction SilentlyContinue | Select-Object -First 1).ProcessName
        if (-not $name) { $name = "unknown" }
        return "$name (PID $owner)"
    }
    $listener = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback), $Port
    try { $listener.Start(); return $null } catch { return "another program" } finally { $listener.Stop() }
}

function Test-TcpReachable([string]$HostName, [int]$Port, [int]$TimeoutMs = 3000) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne($TimeoutMs)) { return $false }
        $client.EndConnect($pending)
        return $true
    } catch { return $false } finally { $client.Close() }
}

function New-Check([string]$Name, [bool]$Ok, [string]$Message, [string]$Fix = "") {
    return [pscustomobject]@{ Name = $Name; Ok = $Ok; Message = $Message; Fix = $Fix }
}

function Get-Settings($Paths) {
    $dotEnv = Read-DotEnvSubset $Paths.DotEnv $EnvKeys
    $identity = (Get-EffectiveSetting $dotEnv "PADOS_IDENTITY_SESSIONS_ENABLED" "false").ToLowerInvariant()
    return [pscustomobject]@{
        DbHost          = Get-EffectiveSetting $dotEnv "PADOS_DB_HOST" "127.0.0.1"
        DbPort          = [int](Get-EffectiveSetting $dotEnv "PADOS_DB_PORT" "5432")
        ApiHost         = Get-EffectiveSetting $dotEnv "PADOS_API_HOST" "127.0.0.1"
        IdentityEnabled = @("true", "1", "yes", "on") -contains $identity
        AllowedOrigins  = @((Get-EffectiveSetting $dotEnv "PADOS_IDENTITY_ALLOWED_ORIGINS" $DefaultOrigins).Split(",") |
            ForEach-Object { $_.Trim() } | Where-Object { $_ })
    }
}

function Get-ToolChecks($Paths) {
    $checks = @()
    $checks += New-Check "python" (Test-Path -LiteralPath $Paths.Python -PathType Leaf) `
        "Python virtual environment .venv\Scripts\python.exe" `
        'py -3.12 -m venv .venv; .\.venv\Scripts\python -m pip install -c requirements.lock -e ".[dev]"'
    $checks += New-Check "npm" ([bool](Get-Command npm.cmd -ErrorAction SilentlyContinue)) "npm.cmd on PATH" `
        "Install Node.js LTS (it includes npm), then open a new PowerShell window."
    $checks += New-Check "node_modules" (Test-Path -LiteralPath $Paths.NodeModules -PathType Container) `
        "Web dependencies web\node_modules" "cd web; npm ci; cd .."
    $checks += New-Check "dotenv" (Test-Path -LiteralPath $Paths.DotEnv -PathType Leaf) `
        "Settings file .env (created once by pados init)" `
        ".\.venv\Scripts\pados.exe init    (run it yourself once; this launcher never runs it)"
    return $checks
}

function Get-RuntimeChecks($Settings, [int]$Api, [int]$Web) {
    $checks = @()
    $checks += New-Check "ports_distinct" ($Api -ne $Web) "API and web ports differ ($Api, $Web)" `
        "Choose two different ports, for example -ApiPort 8088 -WebPort 5173."
    $checks += New-Check "api_host_loopback" ($LoopbackHosts -contains $Settings.ApiHost) `
        "PADOS_API_HOST is a loopback address ($($Settings.ApiHost))" `
        "Set PADOS_API_HOST=127.0.0.1 in .env (pados serve refuses any other host)."
    $db = Test-TcpReachable $Settings.DbHost $Settings.DbPort
    $checks += New-Check "postgresql" $db "PostgreSQL accepts connections at $($Settings.DbHost):$($Settings.DbPort)" `
        "Start your PostgreSQL service (services.msc), or correct PADOS_DB_HOST / PADOS_DB_PORT in .env."
    foreach ($pair in @(@("api_port", $Api, "-ApiPort"), @("web_port", $Web, "-WebPort"))) {
        $owner = Get-PortOwner $pair[1]
        $message = if ($owner) { "PORT_IN_USE: port $($pair[1]) is used by $owner" } else { "Port $($pair[1]) is free" }
        $checks += New-Check $pair[0] (-not $owner) $message `
            "If that is PADOS, run -Status or -Stop; otherwise pick a free port, for example $($pair[2]) $($pair[1] + 2)."
    }
    return $checks
}

function Write-Checks($Checks) {
    foreach ($check in $Checks) {
        if ($check.Ok) { Write-Line " OK " $check.Message; continue }
        Write-Line "FAIL" "$($check.Message): missing or not usable"
        if ($check.Fix) { Write-Host "       Fix: $($check.Fix)" }
    }
}

function Read-PidFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try { $data = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json } catch { return $null }
    if (-not $data -or $data.schema -ne $PidSchema) { return $null }
    return $data
}

function Get-RecordedProcess($Entry) {
    # The live process for a PID file entry, or $null. A PID whose start time differs was reused by another program.
    $process = Get-Process -Id ([int]$Entry.pid) -ErrorAction SilentlyContinue
    if (-not $process) { return [pscustomobject]@{ State = "STOPPED"; Process = $null } }
    $started = $null
    try { $started = $process.StartTime.ToUniversalTime().ToString("o") } catch { }
    if ($started -ne [string]$Entry.start_time) { return [pscustomobject]@{ State = "PID_REUSED"; Process = $null } }
    return [pscustomobject]@{ State = "RUNNING"; Process = $process }
}

function Stop-ProcessTree([int]$ProcessId) {
    # npm and the venv python.exe start child processes; /T stops the whole tree.
    $null = & taskkill.exe /PID $ProcessId /T /F 2>&1
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($process) { $process | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue }
    return -not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Get-ReadyReport([int]$Port) {
    foreach ($hostName in @("127.0.0.1", "localhost")) {
        $request = [System.Net.HttpWebRequest]::Create("http://${hostName}:$Port/health/ready")
        $request.Timeout = 4000
        $request.Proxy = $null
        $response = $null
        try { $response = $request.GetResponse() } catch [System.Net.WebException] { $response = $_.Exception.Response }
        if (-not $response) { continue }
        try {
            $reader = New-Object System.IO.StreamReader ($response.GetResponseStream())
            $body = $reader.ReadToEnd()
            $code = [int]$response.StatusCode
        } finally { $response.Close() }
        $report = $null
        try { $report = $body | ConvertFrom-Json } catch { }
        $state = if ($report -and $report.status) { [string]$report.status } else { "HTTP_$code" }
        $failing = @()
        if ($report -and $report.checks) {
            $failing = @($report.checks | Where-Object { $_.status -ne "OK" } |
                ForEach-Object { "$($_.name)=$($_.status)$(if ($_.reason) { " ($($_.reason))" })" })
        }
        return [pscustomobject]@{ Code = $code; Status = $state; NotOk = $failing }
    }
    return $null
}

function Get-LastErrorLine([string]$Path) {
    # `pados serve` prints its refusal as "...error: <CODE>: <next step>" on standard error.
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "no output" }
    $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
    $refusal = $lines | Where-Object { $_ -match "error:" } | Select-Object -Last 1
    if ($refusal) { return $refusal.Trim() }
    if ($lines.Count -gt 0) { return $lines[-1].Trim() }
    return "no output"
}

function Write-PlanAndHints($Paths, $Settings, [int]$Api, [int]$Web) {
    Write-Host ""
    Write-Host "Plan:"
    Write-Host "  Checkout : $($Paths.Root)"
    Write-Host "  API      : .venv\Scripts\python.exe -m pados.cli.main serve   (PADOS_API_PORT=$Api)"
    Write-Host "             logs runtime\logs\pados-serve.out.log, runtime\logs\pados-serve.err.log"
    Write-Host "  Web      : npm.cmd run dev -- --host 127.0.0.1 --port $Web --strictPort   (in web\, PADOS_WEB_BACKEND_URL=http://127.0.0.1:$Api)"
    Write-Host "             logs runtime\logs\pados-web.out.log, runtime\logs\pados-web.err.log"
    Write-Host "  Wait     : http://127.0.0.1:$Api/health/ready (up to $ReadyTimeoutSeconds s)"
    Write-Host "  Open     : http://127.0.0.1:$Web/operator.html"
    Write-Host "  PIDs     : runtime\local\$PidFileName   (used by -Status and -Stop)"
    if ($Settings.IdentityEnabled -and ($Settings.AllowedOrigins -notcontains "http://127.0.0.1:$Web")) {
        Write-Line "WARN" "Browser pairing will refuse this page (ORIGIN_NOT_ALLOWED): http://127.0.0.1:$Web is not in PADOS_IDENTITY_ALLOWED_ORIGINS."
        Write-Host "       Fix: add http://127.0.0.1:$Web to PADOS_IDENTITY_ALLOWED_ORIGINS in .env (edit it yourself), or use -WebPort 5173."
    }
}

function Write-PairingHint($Settings, [int]$Api, [int]$Web) {
    Write-Host ""
    if ($Settings.IdentityEnabled) {
        Write-Host "Pair this browser (once per browser; then no control token is needed):"
        Write-Host "  1. In a PowerShell window in this checkout run:"
        Write-Host "       .\.venv\Scripts\pados.exe device pair --base-url http://127.0.0.1:$Api"
        Write-Host "  2. In http://127.0.0.1:$Web/operator.html open Control Center, then 'This browser & devices',"
        Write-Host "     choose 'Pair this browser' and type the code (XXXXX-XXXXX). A code works once, for 5 minutes."
    } else {
        Write-Host "Browser pairing is off (PADOS_IDENTITY_SESSIONS_ENABLED is not true): changes in the Control Center ask"
        Write-Host "for the control token from runtime\local\control.token. To pair instead, set"
        Write-Host "PADOS_IDENTITY_SESSIONS_ENABLED=true in .env yourself and start again (this launcher never edits .env)."
    }
}

function Start-Child([string]$CommandLine, [string]$WorkDir, [hashtable]$ChildEnv, [string]$OutLog, [string]$ErrLog) {
    # Started through cmd.exe without the redirection parameters, so that Start-Process uses ShellExecute: the child inherits this
    # process's environment but none of its handles. (With them, Windows PowerShell 5.1 lets the
    # child inherit the launcher's own output pipe, and a caller capturing the launcher's output would wait until
    # pados serve and Vite exit.) cmd.exe writes the logs. Start-Process in 5.1 has no -Environment either, so the
    # variables are set for the start only and then restored.
    $saved = @{}
    foreach ($name in $ChildEnv.Keys) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
    try {
        foreach ($name in $ChildEnv.Keys) { [Environment]::SetEnvironmentVariable($name, $ChildEnv[$name], "Process") }
        $inner = "$CommandLine 1>`"$OutLog`" 2>`"$ErrLog`""
        return Start-Process -FilePath "cmd.exe" -ArgumentList "/d /s /c `"$inner`"" -WorkingDirectory $WorkDir `
            -WindowStyle Hidden -PassThru
    } finally {
        foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process") }
    }
}

function Save-PidFile($Paths, [int]$Api, [int]$Web, $Entries) {
    $document = [ordered]@{
        schema = $PidSchema; root = $Paths.Root; api_port = $Api; web_port = $Web
        written_at = (Get-Date).ToUniversalTime().ToString("o"); processes = @($Entries)
    }
    $temp = "$($Paths.PidFile).tmp"
    $document | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temp -Encoding UTF8
    Move-Item -LiteralPath $temp -Destination $Paths.PidFile -Force
}

function New-PidEntry([string]$Role, [System.Diagnostics.Process]$Process) {
    return [ordered]@{ role = $Role; pid = $Process.Id; start_time = $Process.StartTime.ToUniversalTime().ToString("o") }
}

function Stop-Recorded($Paths) {
    $data = Read-PidFile $Paths.PidFile
    if (-not $data) { Write-Line "INFO" "Nothing to stop: no launcher PID file at runtime\local\$PidFileName."; return 0 }
    $failed = 0
    foreach ($entry in @($data.processes)) {
        $found = Get-RecordedProcess $entry
        if ($found.State -eq "STOPPED") { Write-Line "INFO" "$($entry.role) (PID $($entry.pid)) was already stopped."; continue }
        if ($found.State -eq "PID_REUSED") {
            Write-Line "WARN" "PID_REUSED: PID $($entry.pid) now belongs to another program; it was not stopped."
            continue
        }
        if (Stop-ProcessTree $found.Process.Id) { Write-Line " OK " "Stopped $($entry.role) (PID $($entry.pid))." }
        else { Write-Line "FAIL" "Could not stop $($entry.role) (PID $($entry.pid))."; $failed++ }
    }
    if ($failed -eq 0) { Remove-Item -LiteralPath $Paths.PidFile -Force -ErrorAction SilentlyContinue }
    if ($failed -eq 0) { return 0 } else { return 1 }
}

function Show-Status($Paths) {
    $data = Read-PidFile $Paths.PidFile
    if (-not $data) { Write-Line "INFO" "PADOS is not running from this launcher (no PID file at runtime\local\$PidFileName)."; return 1 }
    $running = 0
    foreach ($entry in @($data.processes)) {
        $found = Get-RecordedProcess $entry
        Write-Line "INFO" ("{0,-4} PID {1,-7} {2}" -f $entry.role, $entry.pid, $found.State)
        if ($found.State -eq "RUNNING") { $running++ }
    }
    $ready = Get-ReadyReport ([int]$data.api_port)
    if ($ready) {
        Write-Line "INFO" "API  http://127.0.0.1:$($data.api_port)/health/ready -> $($ready.Status) (HTTP $($ready.Code))"
        foreach ($item in $ready.NotOk) { Write-Host "       $item" }
    } else { Write-Line "INFO" "API  http://127.0.0.1:$($data.api_port) is not answering." }
    Write-Line "INFO" "Web  http://127.0.0.1:$($data.web_port)/operator.html"
    Write-Line "INFO" "Logs runtime\logs\pados-serve.*.log, runtime\logs\pados-web.*.log"
    if ($running -eq @($data.processes).Count) { return 0 } else { return 1 }
}

function Wait-Stack($Paths, $ApiProcess, $WebProcess, [int]$Api, [int]$Web) {
    # Returns $null when both are up and the API is ready, else the reason.
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    $last = $null
    while ((Get-Date) -lt $deadline) {
        if ($ApiProcess.HasExited) { return "BACKEND_START_REFUSED (exit $($ApiProcess.ExitCode)): $(Get-LastErrorLine $Paths.ApiErr)" }
        $last = Get-ReadyReport $Api
        if ($last -and $last.Code -eq 200) { break }
        Start-Sleep -Milliseconds 750
    }
    if (-not $last -or $last.Code -ne 200) {
        $detail = if ($last) { "$($last.Status) $($last.NotOk -join ', ')" } else { "no answer" }
        return "HEALTH_NOT_READY after $ReadyTimeoutSeconds s: $detail"
    }
    Write-Line " OK " "API ready: $($last.Status)$(if ($last.NotOk.Count) { ' (' + ($last.NotOk -join ', ') + ')' })"
    while ((Get-Date) -lt $deadline.AddSeconds(30)) {
        if ($WebProcess.HasExited) { return "WEB_START_FAILED (exit $($WebProcess.ExitCode)): $(Get-LastErrorLine $Paths.WebErr)" }
        if (Get-PortOwner $Web) { return $null }
        Start-Sleep -Milliseconds 500
    }
    return "WEB_START_FAILED: nothing listens on port $Web"
}

function Start-Stack($Paths, $Settings, [int]$Api, [int]$Web) {
    New-Item -ItemType Directory -Force -Path $Paths.Logs, $Paths.LocalState | Out-Null
    $entries = @()
    $apiProcess = Start-Child "`"$($Paths.Python)`" -m pados.cli.main serve" $Paths.Root `
        @{ PADOS_API_PORT = "$Api" } $Paths.ApiOut $Paths.ApiErr
    $entries += New-PidEntry "api" $apiProcess
    Save-PidFile $Paths $Api $Web $entries
    $webProcess = Start-Child "npm.cmd run dev -- --host 127.0.0.1 --port $Web --strictPort" $Paths.Web `
        @{ PADOS_WEB_BACKEND_URL = "http://127.0.0.1:$Api"; PADOS_WEB_PORT = "$Web" } $Paths.WebOut $Paths.WebErr
    $entries += New-PidEntry "web" $webProcess
    Save-PidFile $Paths $Api $Web $entries
    Write-Line "INFO" "Started pados serve (PID $($apiProcess.Id)) and the web dev server (PID $($webProcess.Id)); waiting..."
    $problem = Wait-Stack $Paths $apiProcess $webProcess $Api $Web
    if ($problem) {
        Write-Line "FAIL" $problem
        Write-Host "       Logs: runtime\logs\pados-serve.err.log, runtime\logs\pados-web.err.log"
        $null = Stop-Recorded $Paths
        return 2
    }
    Write-Line " OK " "PADOS is running. Control Center: http://127.0.0.1:$Web/operator.html"
    Write-Host "       Stop with: .\scripts\windows\Start-PADOS.ps1 -Stop    Status: -Status"
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$Web/operator.html" | Out-Null }
    Write-PairingHint $Settings $Api $Web
    return 0
}

function Invoke-Launcher {
    $startDir = if ($Root) { $Root } else { $PSScriptRoot }
    $rootPath = Find-PadosRoot $startDir
    if (-not $rootPath) {
        Write-Line "FAIL" "CHECKOUT_NOT_FOUND: no PADOS checkout (pyproject.toml + src\pados) at or above $startDir."
        Write-Host "       Fix: run scripts\windows\Start-PADOS.ps1 from inside a PADOS checkout, or pass -Root <checkout folder>."
        return 1
    }
    $paths = Get-LauncherPaths $rootPath
    if ($Stop) { return Stop-Recorded $paths }
    if ($Status) { return Show-Status $paths }
    $existing = Read-PidFile $paths.PidFile
    if ($existing -and @($existing.processes | Where-Object { (Get-RecordedProcess $_).State -eq "RUNNING" }).Count -gt 0) {
        Write-Line "FAIL" "ALREADY_RUNNING: this launcher already started PADOS here (see -Status). Stop it first with -Stop."
        return 1
    }
    Write-Line "INFO" "PADOS checkout: $rootPath$(if ($DryRun) { '   (dry run: nothing will be started)' })"
    $settings = Get-Settings $paths
    $checks = @(Get-ToolChecks $paths) + @(Get-RuntimeChecks $settings $ApiPort $WebPort)
    Write-Checks $checks
    Write-PlanAndHints $paths $settings $ApiPort $WebPort
    $failedCount = @($checks | Where-Object { -not $_.Ok }).Count
    if ($DryRun) {
        Write-PairingHint $settings $ApiPort $WebPort
        Write-Host ""
        Write-Line "INFO" "DRY RUN: nothing was started. $failedCount check(s) failed."
        if ($failedCount -gt 0) { return 1 } else { return 0 }
    }
    if ($failedCount -gt 0) { Write-Line "FAIL" "$failedCount check(s) failed; nothing was started."; return 1 }
    return Start-Stack $paths $settings $ApiPort $WebPort
}

# Dot-sourcing (unit tests) defines the functions only.
if ($MyInvocation.InvocationName -eq ".") { return }
exit (Invoke-Launcher)
