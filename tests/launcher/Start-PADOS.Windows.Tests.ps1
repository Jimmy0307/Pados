# Pester 3.4 tests for scripts/windows/Start-PADOS.ps1 (Windows PowerShell 5.1).
# Every scenario runs against a fake checkout in $TestDrive and uses its own ports (186xx); it never starts PADOS,
# never binds 5173 or 8088, and never touches a real .env or database. The start-path scenarios replace the two child
# command lines with small PowerShell fakes (a loopback health server and a web listener) and run from a checkout
# whose path contains spaces and non-ASCII characters.
$ErrorActionPreference = 'Stop'

$LauncherPath = (Resolve-Path (Join-Path $PSScriptRoot '..\..\scripts\windows\Start-PADOS.ps1')).Path
$WrapperPath = Join-Path (Split-Path -Parent $LauncherPath) 'Start-PADOS.cmd'
$ApiPort = 18611
$WebPort = 18612
$DbPort = 18613
$BusyPort = 18614

function New-FakeCheckout {
    param([string]$Path, [switch]$NoVenv, [switch]$NoNodeModules, [switch]$NoDotEnv, [switch]$IdentityOn,
          [string]$DatabasePort = "$DbPort", [string]$PasswordValue = '')
    Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force -Path (Join-Path $Path 'src\pados'), (Join-Path $Path 'web') | Out-Null
    Set-Content -LiteralPath (Join-Path $Path 'pyproject.toml') -Value '[project]' -Encoding ASCII
    Set-Content -LiteralPath (Join-Path $Path 'src\pados\__init__.py') -Value '' -Encoding ASCII
    if (-not $NoVenv) {
        New-Item -ItemType Directory -Force -Path (Join-Path $Path '.venv\Scripts') | Out-Null
        Set-Content -LiteralPath (Join-Path $Path '.venv\Scripts\python.exe') -Value '' -Encoding ASCII
    }
    if (-not $NoNodeModules) { New-Item -ItemType Directory -Force -Path (Join-Path $Path 'web\node_modules') | Out-Null }
    if (-not $NoDotEnv) {
        $lines = @('PADOS_DB_HOST=127.0.0.1', "PADOS_DB_PORT=$DatabasePort", 'PADOS_API_HOST=127.0.0.1',
            "PADOS_IDENTITY_SESSIONS_ENABLED=$(if ($IdentityOn) { 'true' } else { 'false' })")
        if ($PasswordValue) { $lines += ('PADOS_DB_' + 'PASS' + 'WORD=' + $PasswordValue) }
        Set-Content -LiteralPath (Join-Path $Path '.env') -Value $lines -Encoding UTF8
    }
    return $Path
}

function Invoke-Launcher {
    param([string[]]$Arguments)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $LauncherPath @Arguments 2>&1 |
            ForEach-Object { [string]$_ })
        $code = [int]$LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    return [pscustomobject]@{ ExitCode = $code; Output = ($output -join [Environment]::NewLine) }
}

function Start-FakeDatabase {
    # A loopback TCP listener stands in for PostgreSQL: the launcher only checks that the port accepts connections.
    $listener = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback), $DbPort
    $listener.Start()
    return $listener
}

function Start-Sleeper {
    return Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-Command', 'Start-Sleep -Seconds 120') `
        -WindowStyle Hidden -PassThru
}

function Write-FakePidFile([string]$Checkout, $Entries) {
    $dir = Join-Path $Checkout 'runtime\local'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $doc = [ordered]@{ schema = 'pados.launcher.pids.v1'; root = $Checkout; api_port = $ApiPort; web_port = $WebPort
        written_at = (Get-Date).ToUniversalTime().ToString('o'); processes = @($Entries) }
    $doc | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $dir 'pados-launcher.json') -Encoding UTF8
}

Describe 'Start-PADOS launcher: static guarantees' {
    $source = Get-Content -Raw -Encoding UTF8 $LauncherPath

    It 'never runs pados init and never writes, appends to, moves or deletes .env' {
        $source | Should Not Match '"init"'
        $source | Should Not Match '(Set-Content|Add-Content|Out-File|Clear-Content|Remove-Item|Move-Item|Copy-Item|Rename-Item|New-Item)[^\r\n]*(DotEnv|\.env\b)'
        $source | Should Not Match '\[(System\.)?IO\.File\]::(Write|Append|Delete|Move|Copy|Replace|Create)'
        $source | Should Not Match 'Out-File|Add-Content|WriteAll|AppendAll'
    }

    It 'reads only allowlisted .env keys, none of them secret' {
        $source | Should Match '\$EnvKeys = @\("PADOS_DB_HOST"'
        $source | Should Not Match '"PADOS_DB_PASSWORD"'
        $source | Should Not Match '"PADOS_CONTROL_API_TOKEN_HASH"'
    }

    It 'starts pados serve and Vite on loopback with a strict port' {
        $source | Should Match '-m pados\.cli\.main serve"'
        $source | Should Match 'npm\.cmd run dev -- --host 127\.0\.0\.1 --port \$Web --strictPort'
        $source | Should Not Match '"uvicorn"'
    }

    It 'starts children without -Redirect* so they inherit no launcher handles' {
        $source | Should Not Match '-RedirectStandard'
        $source | Should Match 'Start-Process -FilePath "cmd\.exe"'
    }

    It 'is pure ASCII so Windows PowerShell 5.1 reads it without a BOM' {
        $bytes = [System.IO.File]::ReadAllBytes($LauncherPath)
        @($bytes | Where-Object { $_ -gt 127 }).Count | Should Be 0
    }

    It 'has a cmd wrapper that forwards every argument and the exit code' {
        $wrapper = Get-Content -Raw $WrapperPath
        $wrapper | Should Match 'Start-PADOS\.ps1" %\*'
        $wrapper | Should Match 'exit /b %PADOS_LAUNCHER_EXIT%'
    }
}

Describe 'Start-PADOS launcher: dry-run planning' {
    $listener = Start-FakeDatabase
    try {
        It 'plans both processes, logs, readiness and the console URL, and starts nothing' {
            $sentinel = 'pw-' + [guid]::NewGuid().ToString('N')
            $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'dry') -PasswordValue $sentinel
            $result = Invoke-Launcher @('-DryRun', '-Root', $checkout, '-ApiPort', "$ApiPort", '-WebPort', "$WebPort")
            $result.ExitCode | Should Be 0
            $result.Output | Should Match 'dry run: nothing will be started'
            $result.Output | Should Match "python\.exe -m pados\.cli\.main serve\s+\(PADOS_API_PORT=$ApiPort\)"
            $result.Output | Should Match "npm\.cmd run dev -- --host 127\.0\.0\.1 --port $WebPort --strictPort"
            $result.Output | Should Match "http://127\.0\.0\.1:$ApiPort/health/ready"
            $result.Output | Should Match "http://127\.0\.0\.1:$WebPort/operator\.html"
            $result.Output | Should Match 'runtime\\logs\\pados-serve\.err\.log'
            $result.Output | Should Match "PostgreSQL accepts connections at 127\.0\.0\.1:$DbPort"
            $result.Output | Should Match 'DRY RUN: nothing was started\. 0 check\(s\) failed'
            $result.Output | Should Not Match $sentinel
            Join-Path $checkout 'runtime\local\pados-launcher.json' | Should Not Exist
            Join-Path $checkout 'runtime\logs' | Should Not Exist
            Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue | Should BeNullOrEmpty
        }

        It 'prints the pairing command for the chosen API port when identity sessions are on' {
            $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'pair') -IdentityOn
            $result = Invoke-Launcher @('-DryRun', '-Root', $checkout, '-ApiPort', "$ApiPort", '-WebPort', "$WebPort")
            $result.Output | Should Match "pados\.exe device pair --base-url http://127\.0\.0\.1:$ApiPort"
            $result.Output | Should Match "Pair this browser"
            # 18612 is not in the default allowed origins: the launcher warns instead of editing .env.
            $result.Output | Should Match 'ORIGIN_NOT_ALLOWED'
        }

        It 'explains the token path when identity sessions are off' {
            $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'nopair')
            $result = Invoke-Launcher @('-DryRun', '-Root', $checkout, '-ApiPort', "$ApiPort", '-WebPort', "$WebPort")
            $result.Output | Should Match 'Browser pairing is off'
            $result.Output | Should Not Match 'device pair --base-url'
        }

        It 'refuses the same port for API and web' {
            $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'same')
            $result = Invoke-Launcher @('-DryRun', '-Root', $checkout, '-ApiPort', "$ApiPort", '-WebPort', "$ApiPort")
            $result.ExitCode | Should Be 1
            $result.Output | Should Match '\[FAIL\] API and web ports differ'
        }
    } finally { $listener.Stop() }
}

Describe 'Start-PADOS launcher: missing prerequisites print the exact fix' {
    $listener = Start-FakeDatabase
    try {
        It 'missing .venv' {
            $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'novenv') -NoVenv
            $result = Invoke-Launcher @('-Root', $checkout, '-ApiPort', "$ApiPort", '-WebPort', "$WebPort", '-NoBrowser')
            $result.ExitCode | Should Be 1
            $result.Output | Should Match '\[FAIL\] Python virtual environment \.venv\\Scripts\\python\.exe'
            $result.Output | Should Match 'Fix: py -3\.12 -m venv \.venv; \.\\\.venv\\Scripts\\python -m pip install -c requirements\.lock -e "\.\[dev\]"'
            $result.Output | Should Match 'nothing was started'
            Join-Path $checkout 'runtime\local\pados-launcher.json' | Should Not Exist
        }

        It 'missing web\node_modules' {
            $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'nonode') -NoNodeModules
            $result = Invoke-Launcher @('-Root', $checkout, '-ApiPort', "$ApiPort", '-WebPort', "$WebPort", '-NoBrowser')
            $result.ExitCode | Should Be 1
            $result.Output | Should Match '\[FAIL\] Web dependencies web\\node_modules'
            $result.Output | Should Match 'Fix: cd web; npm ci; cd \.\.'
        }

        It 'missing .env names pados init but does not run it' {
            $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'noenv') -NoDotEnv
            $result = Invoke-Launcher @('-DryRun', '-Root', $checkout, '-ApiPort', "$ApiPort", '-WebPort', "$WebPort")
            $result.Output | Should Match 'this launcher never runs it'
            Join-Path $checkout '.env' | Should Not Exist
        }

        It 'no checkout above the start folder' {
            $empty = Join-Path $TestDrive 'empty'
            New-Item -ItemType Directory -Force -Path $empty | Out-Null
            $result = Invoke-Launcher @('-DryRun', '-Root', $empty, '-ApiPort', "$ApiPort", '-WebPort', "$WebPort")
            $result.ExitCode | Should Be 1
            $result.Output | Should Match 'CHECKOUT_NOT_FOUND'
        }
    } finally { $listener.Stop() }

    It 'PostgreSQL not reachable' {
        $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'nodb')
        $result = Invoke-Launcher @('-DryRun', '-Root', $checkout, '-ApiPort', "$ApiPort", '-WebPort', "$WebPort")
        $result.ExitCode | Should Be 1
        $result.Output | Should Match "\[FAIL\] PostgreSQL accepts connections at 127\.0\.0\.1:$DbPort"
        $result.Output | Should Match 'correct PADOS_DB_HOST / PADOS_DB_PORT in \.env'
    }
}

Describe 'Start-PADOS launcher: port-in-use detection' {
    $db = Start-FakeDatabase
    $busy = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback), $BusyPort
    $busy.Start()
    try {
        It 'refuses a busy API port and names the owner' {
            $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'busyapi')
            $result = Invoke-Launcher @('-Root', $checkout, '-ApiPort', "$BusyPort", '-WebPort', "$WebPort", '-NoBrowser')
            $result.ExitCode | Should Be 1
            $name = (Get-Process -Id $PID).ProcessName
            $result.Output | Should Match "PORT_IN_USE: port $BusyPort is used by $name \(PID $PID\)"
            $result.Output | Should Match 'run -Status or -Stop; otherwise pick a free port, for example -ApiPort'
            Join-Path $checkout 'runtime\local\pados-launcher.json' | Should Not Exist
        }

        It 'refuses a busy web port' {
            $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'busyweb')
            $result = Invoke-Launcher @('-DryRun', '-Root', $checkout, '-ApiPort', "$ApiPort", '-WebPort', "$BusyPort")
            $result.ExitCode | Should Be 1
            $result.Output | Should Match "PORT_IN_USE: port $BusyPort"
            $result.Output | Should Match '-WebPort'
        }
    } finally {
        $busy.Stop()
        $db.Stop()
    }
}

Describe 'Start-PADOS launcher: -Stop and -Status with a PID file' {
    It 'stops a recorded process and removes the PID file' {
        $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'stop')
        $sleeper = Start-Sleeper
        try {
            Write-FakePidFile $checkout @([ordered]@{ role = 'api'; pid = $sleeper.Id
                start_time = $sleeper.StartTime.ToUniversalTime().ToString('o') })
            $status = Invoke-Launcher @('-Status', '-Root', $checkout)
            $status.Output | Should Match "api\s+PID $($sleeper.Id)\s+RUNNING"
            $result = Invoke-Launcher @('-Stop', '-Root', $checkout)
            $result.ExitCode | Should Be 0
            $result.Output | Should Match "Stopped api \(PID $($sleeper.Id)\)"
            Get-Process -Id $sleeper.Id -ErrorAction SilentlyContinue | Should BeNullOrEmpty
            Join-Path $checkout 'runtime\local\pados-launcher.json' | Should Not Exist
        } finally { Stop-Process -Id $sleeper.Id -Force -ErrorAction SilentlyContinue }
    }

    It 'never stops a PID that now belongs to another program' {
        $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'reused')
        $sleeper = Start-Sleeper
        try {
            Write-FakePidFile $checkout @([ordered]@{ role = 'web'; pid = $sleeper.Id
                start_time = '2000-01-01T00:00:00.0000000Z' })
            $result = Invoke-Launcher @('-Stop', '-Root', $checkout)
            $result.Output | Should Match 'PID_REUSED'
            Get-Process -Id $sleeper.Id -ErrorAction SilentlyContinue | Should Not BeNullOrEmpty
        } finally { Stop-Process -Id $sleeper.Id -Force -ErrorAction SilentlyContinue }
    }

    It 'reports an already stopped process and refuses nothing' {
        $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'gone')
        $sleeper = Start-Sleeper
        $entry = [ordered]@{ role = 'api'; pid = $sleeper.Id; start_time = $sleeper.StartTime.ToUniversalTime().ToString('o') }
        Stop-Process -Id $sleeper.Id -Force
        $sleeper.WaitForExit(10000) | Out-Null
        Write-FakePidFile $checkout @($entry)
        $result = Invoke-Launcher @('-Stop', '-Root', $checkout)
        $result.ExitCode | Should Be 0
        $result.Output | Should Match 'was already stopped'
        Join-Path $checkout 'runtime\local\pados-launcher.json' | Should Not Exist
    }

    It 'with no PID file, -Stop has nothing to do and -Status says not running' {
        $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'none')
        (Invoke-Launcher @('-Stop', '-Root', $checkout)).Output | Should Match 'Nothing to stop'
        $status = Invoke-Launcher @('-Status', '-Root', $checkout)
        $status.ExitCode | Should Be 1
        $status.Output | Should Match 'not running from this launcher'
    }

    It 'refuses to start twice while recorded processes run' {
        $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'twice')
        $sleeper = Start-Sleeper
        try {
            Write-FakePidFile $checkout @([ordered]@{ role = 'api'; pid = $sleeper.Id
                start_time = $sleeper.StartTime.ToUniversalTime().ToString('o') })
            $result = Invoke-Launcher @('-DryRun', '-Root', $checkout, '-ApiPort', "$ApiPort", '-WebPort', "$WebPort")
            $result.ExitCode | Should Be 1
            $result.Output | Should Match 'ALREADY_RUNNING'
        } finally { Stop-Process -Id $sleeper.Id -Force -ErrorAction SilentlyContinue }
    }
}

Describe 'Start-PADOS launcher: closed codes and robust inputs' {
    It 'reports a non-numeric PADOS_DB_PORT as SETTING_INVALID' {
        $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'badport') -DatabasePort 'abc'
        $result = Invoke-Launcher @('-DryRun', '-Root', $checkout, '-ApiPort', "$ApiPort", '-WebPort', "$WebPort")
        $result.ExitCode | Should Be 1
        $result.Output | Should Match 'SETTING_INVALID: PADOS_DB_PORT is not a port number'
    }

    It 'refuses a checkout path that cmd.exe would expand' {
        $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'pct%root')
        $result = Invoke-Launcher @('-DryRun', '-Root', $checkout, '-ApiPort', "$ApiPort", '-WebPort', "$WebPort")
        $result.ExitCode | Should Be 1
        $result.Output | Should Match 'ROOT_PATH_UNSUPPORTED'
    }

    It 'treats a malformed PID file as PID_FILE_INVALID and keeps it' {
        $checkout = New-FakeCheckout -Path (Join-Path $TestDrive 'badpid')
        New-Item -ItemType Directory -Force -Path (Join-Path $checkout 'runtime\local') | Out-Null
        $pidFile = Join-Path $checkout 'runtime\local\pados-launcher.json'
        Set-Content -LiteralPath $pidFile -Value '{"schema": "pados.launcher.pids.v1", "processes": [{"pid": "x"}]' -Encoding ASCII
        $stop = Invoke-Launcher @('-Stop', '-Root', $checkout)
        $stop.ExitCode | Should Be 1
        $stop.Output | Should Match 'PID_FILE_INVALID'
        (Invoke-Launcher @('-Status', '-Root', $checkout)).Output | Should Match 'PID_FILE_INVALID'
        (Invoke-Launcher @('-DryRun', '-Root', $checkout, '-ApiPort', "$ApiPort", '-WebPort', "$WebPort")).Output |
            Should Match 'PID_FILE_INVALID'
        $pidFile | Should Exist
    }
}

Describe 'Start-PADOS launcher: functions (dot-sourced)' {
    . $LauncherPath

    It 'compares start times as instants, whether the PID file gives a string (5.1) or a DateTime (7)' {
        $me = Get-Process -Id $PID
        $asString = [pscustomobject]@{ role = 'api'; pid = $PID; start_time = $me.StartTime.ToUniversalTime().ToString('o') }
        $asDate = [pscustomobject]@{ role = 'api'; pid = $PID; start_time = $me.StartTime }
        $asLocalString = [pscustomobject]@{ role = 'api'; pid = $PID; start_time = $me.StartTime.ToString('o') }
        $wrong = [pscustomobject]@{ role = 'api'; pid = $PID; start_time = '2000-01-01T00:00:00.0000000Z' }
        (Get-RecordedProcess $asString).State | Should Be 'RUNNING'
        (Get-RecordedProcess $asDate).State | Should Be 'RUNNING'
        (Get-RecordedProcess $asLocalString).State | Should Be 'RUNNING'
        (Get-RecordedProcess $wrong).State | Should Be 'PID_REUSED'
    }

    It 'accepts only a PADOS readiness report and shows only closed values from it' {
        (ConvertTo-HealthReport 200 '{"status":"OK"}').Status | Should Be 'NOT_PADOS_HEALTH'
        (ConvertTo-HealthReport 200 '{"status":"OK"}').Ready | Should Be $false
        (ConvertTo-HealthReport 200 'not json').Ready | Should Be $false
        $body = '{"schema_version":"pados.health.v1","kind":"readiness","status":"DEGRADED","checks":[' +
            '{"name":"corpus_database","status":"DEGRADED","reason":"CORPUS_UNAVAILABLE"},' +
            '{"name":"chat_provider","status":"NOT_CONFIGURED","reason":"free text from somewhere"}]}'
        $report = ConvertTo-HealthReport 200 $body
        $report.Ready | Should Be $true
        ($report.NotOk -join ';') | Should Be 'corpus_database=DEGRADED (CORPUS_UNAVAILABLE);chat_provider=NOT_CONFIGURED'
    }

    It 'shows only a closed code from a log, never its text' {
        $log = Join-Path $TestDrive 'err.log'
        Set-Content -LiteralPath $log -Encoding UTF8 -Value @('Traceback (most recent call last):',
            '  File "x:\some\private\place.py"', 'Domain error: SAMPLE_REFUSAL_CODE: a sentence that must stay in the log')
        Get-ClosedCodeFromLog $log 'L' | Should Be 'SAMPLE_REFUSAL_CODE (see L)'
        Set-Content -LiteralPath $log -Encoding UTF8 -Value @('something broke at x:\some\private\place.py')
        Get-ClosedCodeFromLog $log 'L' | Should Be 'see log L'
    }
}

Describe 'Start-PADOS launcher: start path with fake children (checkout path with spaces and non-ASCII)' {
    . $LauncherPath
    $NoBrowser = $true
    $ReadyTimeoutSeconds = 20
    $WebReadySeconds = 10
    $SettleMilliseconds = 500
    $FakeApiPort = 18631
    $FakeWebPort = 18632
    $Fancy = New-FakeCheckout -Path (Join-Path $TestDrive ('fake root ' + [char]0x6AA2 + [char]0x67E5))
    $Fakes = Join-Path $Fancy 'fakes'
    New-Item -ItemType Directory -Force -Path $Fakes | Out-Null
    $FakeApi = Join-Path $Fakes 'fake api.ps1'
    $FakeWeb = Join-Path $Fakes 'fake web.ps1'
    Set-Content -LiteralPath $FakeApi -Encoding UTF8 -Value @'
param([int]$Port, [string]$Mode)
if ($Mode -eq 'refuse') { [Console]::Error.WriteLine('Domain error: FAKE_START_REFUSED: a private sentence from the fake'); exit 2 }
$body = '{"schema_version":"pados.health.v1","kind":"readiness","service":"pados-api","version":"0","status":"OK","checks":[]}'
if ($Mode -eq 'notpados') { $body = '{"status":"OK"}' }
$listener = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback), $Port
$listener.Start()
while ($true) {
    $client = $listener.AcceptTcpClient()
    try {
        $stream = $client.GetStream()
        $reader = New-Object System.IO.StreamReader ($stream)
        while (($line = $reader.ReadLine()) -ne $null -and $line -ne '') { }
        $bytes = [Text.Encoding]::UTF8.GetBytes($body)
        $head = [Text.Encoding]::ASCII.GetBytes("HTTP/1.1 200 OK`r`nContent-Type: application/json`r`nContent-Length: $($bytes.Length)`r`nConnection: close`r`n`r`n")
        $stream.Write($head, 0, $head.Length); $stream.Write($bytes, 0, $bytes.Length); $stream.Flush()
    } catch { } finally { $client.Close() }
}
'@
    Set-Content -LiteralPath $FakeWeb -Encoding UTF8 -Value @'
param([int]$Port, [string]$Mode)
if ($Mode -eq 'exit') { [Console]::Error.WriteLine('npm ERR! a private sentence from the fake web'); exit 1 }
if ($Mode -eq 'listen') {
    $listener = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback), $Port
    $listener.Start()
}
Start-Sleep -Seconds 300
'@

    function Get-ChildCommandLines($Paths, [int]$Api, [int]$Web) {
        $ps = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File'
        return [pscustomobject]@{
            Api = "$ps `"$FakeApi`" -Port $Api -Mode $FakeApiMode"
            Web = "$ps `"$FakeWeb`" -Port $Web -Mode $FakeWebMode"
        }
    }

    function Invoke-FakeStart($StartPaths) {
        $settings = Get-Settings $StartPaths
        $records = @(Start-Stack $StartPaths $settings $FakeApiPort $FakeWebPort 6>&1)
        return [pscustomobject]@{
            Code = ($records | Where-Object { $_ -is [int] } | Select-Object -Last 1)
            Text = (($records | Where-Object { $_ -isnot [int] } | ForEach-Object { "$_" }) -join "`n")
        }
    }

    function Test-Listening([int]$Port) { return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) }

    $Paths = Get-LauncherPaths $Fancy

    It 'starts both children, waits for health and port ownership, records PIDs and logs, and -Stop kills the trees' {
        $FakeApiMode = 'serve'; $FakeWebMode = 'listen'
        $run = Invoke-FakeStart $Paths
        $run.Code | Should Be 0
        $run.Text | Should Match 'PADOS is running'
        Test-Listening $FakeApiPort | Should Be $true
        Test-Listening $FakeWebPort | Should Be $true
        $data = Get-Content -Raw -LiteralPath $Paths.PidFile | ConvertFrom-Json
        (@($data.processes | ForEach-Object { $_.role }) -join ',') | Should Be 'api,web,api-server,web-server'
        $Paths.ApiOut | Should Exist
        $Paths.WebErr | Should Exist
        $recorded = @($data.processes | ForEach-Object { [int]$_.pid })
        Stop-Recorded $Paths 6>$null | Should Be 0
        Start-Sleep -Milliseconds 500
        Test-Listening $FakeApiPort | Should Be $false
        Test-Listening $FakeWebPort | Should Be $false
        @($recorded | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue }).Count | Should Be 0
        $Paths.PidFile | Should Not Exist
    }

    It 'reports a refused backend by its closed code only and stops the web child' {
        $FakeApiMode = 'refuse'; $FakeWebMode = 'listen'
        $run = Invoke-FakeStart $Paths
        $run.Code | Should Be 2
        $run.Text | Should Match 'BACKEND_START_REFUSED \(exit 2\): FAKE_START_REFUSED \(see runtime\\logs\\pados-serve\.err\.log\)'
        $run.Text | Should Not Match 'private sentence'
        $run.Text | Should Match 'stopping what this launcher started'
        Start-Sleep -Milliseconds 500
        Test-Listening $FakeWebPort | Should Be $false
        $Paths.PidFile | Should Not Exist
    }

    It 'does not count a non-PADOS HTTP 200 as ready' {
        $FakeApiMode = 'notpados'; $FakeWebMode = 'listen'
        $ReadyTimeoutSeconds = 5
        $run = Invoke-FakeStart $Paths
        $run.Code | Should Be 2
        $run.Text | Should Match 'HEALTH_NOT_READY after 5 s: NOT_PADOS_HEALTH'
        Start-Sleep -Milliseconds 500
        Test-Listening $FakeApiPort | Should Be $false
        Test-Listening $FakeWebPort | Should Be $false
    }

    It 'does not count a web port held by another program as started' {
        $FakeApiMode = 'serve'; $FakeWebMode = 'sleep'
        $foreign = New-Object System.Net.Sockets.TcpListener ([System.Net.IPAddress]::Loopback), $FakeWebPort
        $foreign.Start()
        try {
            $run = Invoke-FakeStart $Paths
            $run.Code | Should Be 2
            $run.Text | Should Match "PORT_TAKEN_BY_OTHER: port $FakeWebPort"
        } finally { $foreign.Stop() }
        Start-Sleep -Milliseconds 500
        Test-Listening $FakeApiPort | Should Be $false
        $Paths.PidFile | Should Not Exist
    }

    It 'reports a web child that exits without echoing its output, and stops the API' {
        $FakeApiMode = 'serve'; $FakeWebMode = 'exit'
        $run = Invoke-FakeStart $Paths
        $run.Code | Should Be 2
        $run.Text | Should Match 'WEB_START_FAILED \(exit 1\): see log runtime\\logs\\pados-web\.err\.log'
        $run.Text | Should Not Match 'private sentence'
        Start-Sleep -Milliseconds 500
        Test-Listening $FakeApiPort | Should Be $false
    }

    It 'stops the API when starting the web child throws' {
        $FakeApiMode = 'serve'; $FakeWebMode = 'listen'
        $broken = $Paths.PSObject.Copy()
        $broken.Web = Join-Path $Fancy 'no such web folder'
        $run = Invoke-FakeStart $broken
        $run.Code | Should Be 2
        $run.Text | Should Match 'START_INTERRUPTED'
        $run.Text | Should Match 'Stopped api'
        Start-Sleep -Milliseconds 500
        Test-Listening $FakeApiPort | Should Be $false
        $Paths.PidFile | Should Not Exist
    }
}
