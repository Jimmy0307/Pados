# Pester 3.4 tests for scripts/windows/Start-PADOS.ps1 (Windows PowerShell 5.1).
# Every scenario runs against a fake checkout in $TestDrive and uses its own ports (186xx); it never starts PADOS,
# never binds 5173 or 8088, and never touches a real .env or database.
$ErrorActionPreference = 'Stop'

$LauncherPath = (Resolve-Path (Join-Path $PSScriptRoot '..\..\scripts\windows\Start-PADOS.ps1')).Path
$WrapperPath = Join-Path (Split-Path -Parent $LauncherPath) 'Start-PADOS.cmd'
$ApiPort = 18611
$WebPort = 18612
$DbPort = 18613
$BusyPort = 18614

function New-FakeCheckout {
    param([string]$Path, [switch]$NoVenv, [switch]$NoNodeModules, [switch]$NoDotEnv, [switch]$IdentityOn,
          [int]$DatabasePort = $DbPort, [string]$PasswordValue = '')
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

    It 'never runs pados init and never writes .env' {
        $source | Should Not Match '"init"'
        $source | Should Not Match "Set-Content[^\r\n]*DotEnv"
        $source | Should Not Match "Out-File"
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
            $result.Output | Should Match "PORT_IN_USE: port $BusyPort is used by powershell \(PID $PID\)"
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
