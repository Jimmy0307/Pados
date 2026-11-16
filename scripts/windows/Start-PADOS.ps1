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
      6. waits until the API port is held by its own child and /health/ready answers a PADOS readiness report,
         and until the web port is held by its own child; then opens http://127.0.0.1:<WebPort>/operator.html and
         prints how to pair the browser when identity sessions are enabled.
    Any failure or interruption (including Ctrl+C) during the start stops what was started.
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
$WebReadySeconds = 60          # how long the web dev server may take after the API is ready
$SettleMilliseconds = 1500     # both children must still be alive this long after they answered
$ClosedCode = '[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+'
$HealthStates = @("OK", "DEGRADED", "NOT_READY")
$CheckStates = @("OK", "DEGRADED", "FAILED", "NOT_CONFIGURED")

function Write-Line([string]$Tag, [string]$Text) { Write-Host ("[{0}] {1}" -f $Tag, $Text) }

function Get-Prop($Object, [string]$Name) {
    # StrictMode-safe property read for parsed JSON: $null when absent.
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) { return $property.Value }
    return $null
}

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

function Get-ListenerIds([int]$Port) {
    if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) { return @() }
    return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
}

function Get-PortOwner([int]$Port) {
    # Returns $null when nothing listens on the port (any local address), else a short owner description.
    $owners = @(Get-ListenerIds $Port)
    if ($owners.Count -gt 0) {
        $name = (Get-Process -Id $owners[0] -ErrorAction SilentlyContinue | Select-Object -First 1).ProcessName
        if (-not $name) { $name = "unknown" }
        return "$name (PID $($owners[0]))"
    }
    $listener = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback), $Port
    try { $listener.Start(); return $null } catch { return "another program" } finally { $listener.Stop() }
}

function Get-ParentMap {
    $map = @{}
    foreach ($item in @(Get-CimInstance -ClassName Win32_Process -Property ProcessId, ParentProcessId -ErrorAction SilentlyContinue)) {
        $map[[int]$item.ProcessId] = [int]$item.ParentProcessId
    }
    return $map
}

function Test-DescendantOf([int]$ProcessId, [int]$AncestorId, [hashtable]$ParentMap) {
    $current = $ProcessId
    for ($depth = 0; $depth -lt 32; $depth++) {
        if ($current -eq $AncestorId) { return $true }
        if (-not $ParentMap.ContainsKey($current)) { return $false }
        $next = $ParentMap[$current]
        if ($next -le 0 -or $next -eq $current) { return $false }
        $current = $next
    }
    return $false
}

function Get-OwnedListener([int]$Port, [int]$AncestorId) {
    # The PID listening on $Port when it is $AncestorId or one of its descendants; 0 when nothing listens;
    # -1 when only processes this launcher did not start listen there.
    $owners = @(Get-ListenerIds $Port)
    if ($owners.Count -eq 0) { return 0 }
    $map = Get-ParentMap
    foreach ($owner in $owners) { if (Test-DescendantOf $owner $AncestorId $map) { return $owner } }
    return -1
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
    $dbPort = 0
    $dbPortValid = [int]::TryParse((Get-EffectiveSetting $dotEnv "PADOS_DB_PORT" "5432"), [ref]$dbPort) -and
        $dbPort -ge 1 -and $dbPort -le 65535
    return [pscustomobject]@{
        DbHost          = Get-EffectiveSetting $dotEnv "PADOS_DB_HOST" "127.0.0.1"
        DbPort          = $dbPort
        DbPortValid     = $dbPortValid
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
        "PADOS_API_HOST is a loopback address" `
        "Set PADOS_API_HOST=127.0.0.1 in .env (pados serve refuses any other host)."
    if (-not $Settings.DbPortValid) {
        $checks += New-Check "postgresql" $false "SETTING_INVALID: PADOS_DB_PORT is not a port number" `
            "Set PADOS_DB_PORT in .env to the port of your PostgreSQL server (for example 5432)."
    } else {
        $db = Test-TcpReachable $Settings.DbHost $Settings.DbPort
        $checks += New-Check "postgresql" $db "PostgreSQL accepts connections at $($Settings.DbHost):$($Settings.DbPort)" `
            "Start your PostgreSQL service (services.msc), or correct PADOS_DB_HOST / PADOS_DB_PORT in .env."
    }
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

function ConvertTo-UtcTicks($Value) {
    # PowerShell 5.1 keeps the round-trip string from ConvertFrom-Json; PowerShell 7 turns it into a [DateTime].
    if ($Value -is [DateTime]) { return $Value.ToUniversalTime().Ticks }
    $parsed = [DateTime]::MinValue
    $styles = [System.Globalization.DateTimeStyles]::RoundtripKind
    if ([DateTime]::TryParse([string]$Value, [System.Globalization.CultureInfo]::InvariantCulture, $styles, [ref]$parsed)) {
        return $parsed.ToUniversalTime().Ticks
    }
    return $null
}

function Test-PidEntry($Entry) {
    $id = 0
    return ((Get-Prop $Entry "role") -is [string]) -and [int]::TryParse([string](Get-Prop $Entry "pid"), [ref]$id) -and
        $id -gt 0 -and $null -ne (ConvertTo-UtcTicks (Get-Prop $Entry "start_time"))
}

function Read-PidFile([string]$Path) {
    # $null when there is no PID file; an object with Invalid = $true when it cannot be trusted.
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $data = $null
    try { $data = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json } catch { }
    $valid = ($null -ne $data) -and ((Get-Prop $data "schema") -eq $PidSchema) -and ($null -ne (Get-Prop $data "processes"))
    $port = 0
    if ($valid) {
        $valid = [int]::TryParse([string](Get-Prop $data "api_port"), [ref]$port) -and
            [int]::TryParse([string](Get-Prop $data "web_port"), [ref]$port)
    }
    if ($valid) { foreach ($entry in @($data.processes)) { if (-not (Test-PidEntry $entry)) { $valid = $false } } }
    if (-not $valid) { return [pscustomobject]@{ Invalid = $true } }
    return $data
}

function Test-PidFileInvalid($Data) { return ($null -ne $Data) -and ((Get-Prop $Data "Invalid") -eq $true) }

function Write-PidFileInvalid {
    Write-Line "FAIL" "PID_FILE_INVALID: runtime\local\$PidFileName cannot be read as a launcher PID file; nothing was stopped or started."
    Write-Host "       Fix: check in Task Manager that no PADOS python/node process from this checkout runs, then delete that file."
}

function Get-RecordedProcessOnce($Entry) {
    $process = Get-Process -Id ([int](Get-Prop $Entry "pid")) -ErrorAction SilentlyContinue
    if (-not $process) { return [pscustomobject]@{ State = "STOPPED"; Process = $null } }
    try { if ($process.HasExited) { return [pscustomobject]@{ State = "STOPPED"; Process = $null } } } catch { }
    $actual = $null
    try { $actual = $process.StartTime.ToUniversalTime().Ticks } catch { }
    if ($null -eq $actual -or $actual -ne (ConvertTo-UtcTicks (Get-Prop $Entry "start_time"))) {
        return [pscustomobject]@{ State = "PID_REUSED"; Process = $null }
    }
    return [pscustomobject]@{ State = "RUNNING"; Process = $process }
}

function Get-RecordedProcess($Entry) {
    # The live process for a PID file entry. A PID whose start time differs was reused by another program. A process
    # that is still exiting (a child of a tree just stopped) can briefly look reused, so that answer is checked twice.
    $found = Get-RecordedProcessOnce $Entry
    if ($found.State -ne "PID_REUSED") { return $found }
    Start-Sleep -Milliseconds 500
    return Get-RecordedProcessOnce $Entry
}

function Stop-ProcessTree([int]$ProcessId) {
    # cmd.exe, npm and the venv python.exe start child processes; /T stops the whole tree.
    $null = & taskkill.exe /PID $ProcessId /T /F 2>&1
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($process) { $process | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue }
    return -not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function ConvertTo-HealthReport([int]$Code, [string]$Body) {
    # Only a PADOS readiness report (pados.health.v1) counts; anything else is NOT_PADOS_HEALTH. Only closed values
    # (state names, check names, UPPER_SNAKE reasons) are kept for display.
    $report = $null
    try { $report = $Body | ConvertFrom-Json } catch { }
    $state = [string](Get-Prop $report "status")
    $isPados = ((Get-Prop $report "schema_version") -eq "pados.health.v1") -and ((Get-Prop $report "kind") -eq "readiness") -and
        ($HealthStates -contains $state)
    if (-not $isPados) { return [pscustomobject]@{ Code = $Code; Status = "NOT_PADOS_HEALTH"; NotOk = @(); Ready = $false } }
    $notOk = @()
    foreach ($check in @(Get-Prop $report "checks")) {
        $name = [string](Get-Prop $check "name"); $checkState = [string](Get-Prop $check "status")
        if ($name -notmatch '^[a-z][a-z0-9_]{0,63}$' -or $CheckStates -notcontains $checkState -or $checkState -eq "OK") { continue }
        $reason = [string](Get-Prop $check "reason")
        $suffix = if ($reason -cmatch "^$ClosedCode$" -and $reason.Length -le 80) { " ($reason)" } else { "" }
        $notOk += "$name=$checkState$suffix"
    }
    return [pscustomobject]@{ Code = $Code; Status = $state; NotOk = $notOk; Ready = ($Code -eq 200) }
}

function Get-ReadyReport([int]$Port) {
    foreach ($hostName in @("127.0.0.1", "localhost")) {
        $request = [System.Net.HttpWebRequest]::Create("http://${hostName}:$Port/health/ready")
        $request.Timeout = 4000
        $request.Proxy = $null
        $response = $null
        try { $response = $request.GetResponse() } catch [System.Net.WebException] { $response = $_.Exception.Response } catch { }
        if (-not $response) { continue }
        try {
            $reader = New-Object System.IO.StreamReader ($response.GetResponseStream())
            $body = $reader.ReadToEnd()
            $code = [int]$response.StatusCode
        } finally { $response.Close() }
        return ConvertTo-HealthReport $code $body
    }
    return $null
}

function Get-ClosedCodeFromLog([string]$Path, [string]$Display) {
    # Never echoes log text: only a closed code ("...error: CODE..." as `pados serve` prints refusals, or a line that
    # is only a code) is shown, otherwise the log path.
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return "no output (see $Display)" }
    $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8 -ErrorAction SilentlyContinue)
    [array]::Reverse($lines)
    foreach ($line in $lines) {
        if ($line -cmatch "error:\s*($ClosedCode)") { return "$($Matches[1].Substring(0, [Math]::Min(80, $Matches[1].Length))) (see $Display)" }
        if ($line -cmatch "^\s*($ClosedCode)\s*$") { return "$($Matches[1].Substring(0, [Math]::Min(80, $Matches[1].Length))) (see $Display)" }
    }
    return "see log $Display"
}

function Get-ExitText($Process) {
    try { if ($null -ne $Process.ExitCode) { return "exit $($Process.ExitCode)" } } catch { }
    return "exited"
}

function Write-PlanAndHints($Paths, $Settings, [int]$Api, [int]$Web) {
    Write-Host ""
    Write-Host "Plan:"
    Write-Host "  Checkout : $($Paths.Root)"
    Write-Host "  API      : .venv\Scripts\python.exe -m pados.cli.main serve   (PADOS_API_PORT=$Api)"
    Write-Host "             logs runtime\logs\pados-serve.out.log, runtime\logs\pados-serve.err.log"
    Write-Host "  Web      : npm.cmd run dev -- --host 127.0.0.1 --port $Web --strictPort   (in web\, PADOS_WEB_BACKEND_URL=http://127.0.0.1:$Api)"
    Write-Host "             logs runtime\logs\pados-web.out.log, runtime\logs\pados-web.err.log"
    Write-Host "  Wait     : port $Api held by pados serve and http://127.0.0.1:$Api/health/ready (up to $ReadyTimeoutSeconds s)"
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

function Get-ChildCommandLines($Paths, [int]$Api, [int]$Web) {
    # Full, quoted paths: cmd.exe would otherwise search the working directory before PATH for npm.cmd.
    $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
    return [pscustomobject]@{
        Api = "`"$($Paths.Python)`" -m pados.cli.main serve"
        Web = "`"$npm`" run dev -- --host 127.0.0.1 --port $Web --strictPort"
    }
}

function Start-Child([string]$CommandLine, [string]$WorkDir, [hashtable]$ChildEnv, [string]$OutLog, [string]$ErrLog) {
    # Started through cmd.exe without the redirection parameters, so that Start-Process uses ShellExecute: the child
    # inherits this process's environment but none of its handles. (With them, Windows PowerShell 5.1 lets the child
    # inherit the launcher's own output pipe, and a caller capturing the launcher's output would wait until pados
    # serve and Vite exit.) cmd.exe writes the logs. Start-Process in 5.1 has no -Environment either, so the
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
    if (Test-PidFileInvalid $data) { Write-PidFileInvalid; return 1 }
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
    if (Test-PidFileInvalid $data) { Write-PidFileInvalid; return 1 }
    $running = 0
    foreach ($entry in @($data.processes)) {
        $found = Get-RecordedProcess $entry
        Write-Line "INFO" ("{0,-10} PID {1,-7} {2}" -f $entry.role, $entry.pid, $found.State)
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

function New-WaitResult([string]$Problem, [int]$ApiListener = 0, [int]$WebListener = 0) {
    return [pscustomobject]@{ Problem = $Problem; ApiListener = $ApiListener; WebListener = $WebListener }
}

function Wait-ApiReady($Paths, $ApiProcess, [int]$Api) {
    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
    $last = $null
    $listener = 0
    while ((Get-Date) -lt $deadline) {
        if ($ApiProcess.HasExited) {
            return New-WaitResult "BACKEND_START_REFUSED ($(Get-ExitText $ApiProcess)): $(Get-ClosedCodeFromLog $Paths.ApiErr 'runtime\logs\pados-serve.err.log')"
        }
        $listener = Get-OwnedListener $Api $ApiProcess.Id
        if ($listener -lt 0) { return New-WaitResult "PORT_TAKEN_BY_OTHER: port $Api is held by a process this launcher did not start" }
        if ($listener -gt 0) {
            $last = Get-ReadyReport $Api
            if ($last -and $last.Ready) {
                $notOk = if ($last.NotOk.Count) { " (" + ($last.NotOk -join ", ") + ")" } else { "" }
                Write-Line " OK " "API ready: $($last.Status)$notOk; port $Api held by pados serve (PID $listener)"
                return New-WaitResult $null $listener
            }
        }
        Start-Sleep -Milliseconds 750
    }
    $detail = if ($last) { "$($last.Status) $($last.NotOk -join ', ')".Trim() } elseif ($listener -gt 0) { "no answer" } else { "port $Api not opened by pados serve" }
    return New-WaitResult "HEALTH_NOT_READY after $ReadyTimeoutSeconds s: $detail (see runtime\logs\pados-serve.err.log)"
}

function Wait-Stack($Paths, $ApiProcess, $WebProcess, [int]$Api, [int]$Web) {
    # Ready means: the API port is held by the API child (or a descendant) and answers a PADOS readiness report with
    # HTTP 200; the web port is held by the web child (or a descendant); both are still alive after a short settle.
    $apiResult = Wait-ApiReady $Paths $ApiProcess $Api
    if ($apiResult.Problem) { return $apiResult }
    $deadline = (Get-Date).AddSeconds($WebReadySeconds)
    $webListener = 0
    while ((Get-Date) -lt $deadline -and $webListener -eq 0) {
        if ($WebProcess.HasExited) {
            return New-WaitResult "WEB_START_FAILED ($(Get-ExitText $WebProcess)): $(Get-ClosedCodeFromLog $Paths.WebErr 'runtime\logs\pados-web.err.log')"
        }
        $webListener = Get-OwnedListener $Web $WebProcess.Id
        if ($webListener -lt 0) { return New-WaitResult "PORT_TAKEN_BY_OTHER: port $Web is held by a process this launcher did not start" }
        if ($webListener -eq 0) { Start-Sleep -Milliseconds 500 }
    }
    if ($webListener -le 0) { return New-WaitResult "WEB_START_FAILED: the web dev server did not open port $Web (see runtime\logs\pados-web.err.log)" }
    Write-Line " OK " "Web dev server listening: port $Web held by its own child (PID $webListener)"
    Start-Sleep -Milliseconds $SettleMilliseconds
    foreach ($check in @(@($ApiProcess, $apiResult.ApiListener, "pados serve"), @($WebProcess, $webListener, "web dev server"))) {
        if ($check[0].HasExited -or -not (Get-Process -Id $check[1] -ErrorAction SilentlyContinue)) {
            return New-WaitResult "START_NOT_STABLE: the $($check[2]) exited right after it answered (see runtime\logs\)"
        }
    }
    return New-WaitResult $null $apiResult.ApiListener $webListener
}

function Start-Stack($Paths, $Settings, [int]$Api, [int]$Web) {
    New-Item -ItemType Directory -Force -Path $Paths.Logs, $Paths.LocalState | Out-Null
    $commands = Get-ChildCommandLines $Paths $Api $Web
    $entries = New-Object System.Collections.ArrayList
    $completed = $false
    try {
        $apiProcess = Start-Child $commands.Api $Paths.Root @{ PADOS_API_PORT = "$Api" } $Paths.ApiOut $Paths.ApiErr
        [void]$entries.Add((New-PidEntry "api" $apiProcess))
        Save-PidFile $Paths $Api $Web $entries
        $webProcess = Start-Child $commands.Web $Paths.Web `
            @{ PADOS_WEB_BACKEND_URL = "http://127.0.0.1:$Api"; PADOS_WEB_PORT = "$Web" } $Paths.WebOut $Paths.WebErr
        [void]$entries.Add((New-PidEntry "web" $webProcess))
        Save-PidFile $Paths $Api $Web $entries
        Write-Line "INFO" "Started pados serve (PID $($apiProcess.Id)) and the web dev server (PID $($webProcess.Id)); waiting..."
        $wait = Wait-Stack $Paths $apiProcess $webProcess $Api $Web
        if ($wait.Problem) {
            Write-Line "FAIL" $wait.Problem
            return 2
        }
        # Also record the processes that hold the ports (python, node), so -Stop reaches them even if cmd.exe is gone.
        foreach ($pair in @(@("api-server", $wait.ApiListener), @("web-server", $wait.WebListener))) {
            $listening = Get-Process -Id $pair[1] -ErrorAction SilentlyContinue
            if ($listening) { [void]$entries.Add((New-PidEntry $pair[0] $listening)) }
        }
        Save-PidFile $Paths $Api $Web $entries
        $completed = $true
    } catch {
        Write-Line "FAIL" "START_INTERRUPTED: $($_.Exception.GetType().Name) while starting."
        return 2
    } finally {
        if (-not $completed) {
            # Also reached on Ctrl+C: nothing this launcher started is left running.
            Write-Line "INFO" "The start did not complete; stopping what this launcher started."
            $null = Stop-Recorded $Paths
        }
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
    if ($rootPath -match '[%!]') {
        # cmd.exe would expand % and ! in the child command lines.
        Write-Line "FAIL" "ROOT_PATH_UNSUPPORTED: the checkout path contains % or !, which cmd.exe would expand."
        Write-Host "       Fix: clone or move the checkout to a folder whose path has no % or ! characters."
        return 1
    }
    $existing = Read-PidFile $paths.PidFile
    if (Test-PidFileInvalid $existing) { Write-PidFileInvalid; return 1 }
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
