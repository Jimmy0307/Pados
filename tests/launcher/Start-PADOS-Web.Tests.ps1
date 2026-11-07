$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

Describe "Start-PADOS-Web launcher" {
    It "keeps both services local and cleans selected ports" {
        $source = Get-Content -Raw -Encoding UTF8 (Join-Path $Root "Start-PADOS-Web.ps1")
        $source | Should Match "127\.0\.0\.1"
        $source | Should Match "Find-FreePort"
        $source | Should Match "Stop-PortListener"
        $source | Should Not Match "postgresql\+?psycopg://"
    }

    It "opens the local operator console, not the Remote sign-in" {
        $source = Get-Content -Raw -Encoding UTF8 (Join-Path $Root "Start-PADOS-Web.ps1")
        $source | Should Match 'Start-Process "\$FrontendUrl/operator\.html"'
    }

    It "is reachable from the existing slash launcher" {
        $source = Get-Content -Raw -Encoding UTF8 (Join-Path $Root "Start-PADOS.ps1")
        $source | Should Match 'LauncherCommand -eq "/web"'
        $source | Should Match "Start-PADOS-Web\.ps1"
    }
}

Describe "Start-PADOS-Web backend start goes through pados serve" {
    $Python = Join-Path $Root ".venv\Scripts\python.exe"
    $LauncherPath = Join-Path $Root "Start-PADOS-Web.ps1"
    . $LauncherPath
    $Logs = Join-Path ([System.IO.Path]::GetTempPath()) ("pados-launcher-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $Logs | Out-Null
    $DefaultHash = "85B427ADA27FF59781973AB29E5F90991D5ABA85771991E0B2D4F9169AB08492"

    function Use-Env([hashtable]$Values, [scriptblock]$Body) {
        $saved = @{}
        foreach ($name in $Values.Keys) { $saved[$name] = [Environment]::GetEnvironmentVariable($name, "Process") }
        try {
            foreach ($name in $Values.Keys) { [Environment]::SetEnvironmentVariable($name, $Values[$name], "Process") }
            & $Body
        } finally {
            foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], "Process") }
        }
    }

    It "uses the documented start, not uvicorn directly" {
        $source = Get-Content -Raw -Encoding UTF8 $LauncherPath
        $source | Should Match '"-m", "pados\.cli\.main", "serve"'
        $source | Should Not Match '"uvicorn"'
    }

    It "refuses the shipped default control token and reports why" {
        Use-Env @{ PADOS_CONTROL_API_TOKEN_HASH = $DefaultHash } {
            $port = Find-FreePort 18760
            { Start-PadosBackend $Python $Root $port (Join-Path $Logs "a.out") (Join-Path $Logs "a.err") } |
                Should Throw "DEFAULT_CONTROL_TOKEN_REFUSED"
            Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
                Should BeNullOrEmpty
        }
    }

    It "refuses a non-loopback host" {
        $hash = (Get-FileHash -Algorithm SHA256 -InputStream ([IO.MemoryStream]::new([Text.Encoding]::UTF8.GetBytes([guid]::NewGuid().ToString())))).Hash
        Use-Env @{ PADOS_CONTROL_API_TOKEN_HASH = $hash; PADOS_API_HOST = "0.0.0.0" } {
            { Start-PadosBackend $Python $Root (Find-FreePort 18770) (Join-Path $Logs "b.out") (Join-Path $Logs "b.err") } |
                Should Throw "control api must bind to localhost"
        }
    }

    It "serves on the chosen port with a unique token and restores PADOS_API_PORT" {
        $hash = (Get-FileHash -Algorithm SHA256 -InputStream ([IO.MemoryStream]::new([Text.Encoding]::UTF8.GetBytes([guid]::NewGuid().ToString())))).Hash
        Use-Env @{ PADOS_CONTROL_API_TOKEN_HASH = $hash; PADOS_API_HOST = "127.0.0.1"; PADOS_API_PORT = "9" } {
            $port = Find-FreePort 18780
            $process = Start-PadosBackend $Python $Root $port (Join-Path $Logs "c.out") (Join-Path $Logs "c.err")
            try {
                (Invoke-RestMethod -Uri "http://127.0.0.1:$port/health/live" -TimeoutSec 5).status | Should Be "ALIVE"
                $env:PADOS_API_PORT | Should Be "9"
            } finally {
                Stop-Child $process
            }
        }
    }

    Remove-Item -Recurse -Force -LiteralPath $Logs -ErrorAction SilentlyContinue
}
