$ErrorActionPreference = 'Stop'

$launcherPath = Join-Path $PSScriptRoot '..\..\Start-PADOS.ps1'

function Initialize-FakeRuntimeCli {
    $fakeCli = Join-Path $TestDrive 'pados.cmd'
    @'
@echo off
if /I "%~1"=="--help" exit /b 0
exit /b 0
'@ | Set-Content -LiteralPath $fakeCli -Encoding ASCII
}

function Initialize-FakeDocker {
    $fakeDocker = Join-Path $TestDrive 'docker.cmd'
    $marker = Join-Path $TestDrive 'docker-called.txt'
    @"
@echo off
echo called>$marker
exit /b 0
"@ | Set-Content -LiteralPath $fakeDocker -Encoding ASCII
}

function Invoke-LauncherRuntimeScenario {
    param(
        [Parameter(Mandatory)]
        [string]$RuntimeMode,
        [switch]$NoDocker
    )

    $oldRuntimeMode = $env:PADOS_RUNTIME_MODE
    $oldPath = $env:PATH
    try {
        $env:PADOS_RUNTIME_MODE = $RuntimeMode
        $env:PATH = "$TestDrive;$oldPath"
        $arguments = @('-ProjectRoot', $TestDrive)
        if ($NoDocker) {
            $arguments += '-NoDocker'
        }
        $output = @(
            @('/exit') |
                & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $launcherPath @arguments 2>&1 |
                ForEach-Object { [string]$_ }
        )
        [pscustomobject]@{
            ExitCode = [int]$LASTEXITCODE
            Output = [string]($output -join [Environment]::NewLine)
        }
    }
    finally {
        $env:PADOS_RUNTIME_MODE = $oldRuntimeMode
        $env:PATH = $oldPath
    }
}

Describe 'Start-PADOS runtime selection' {
    BeforeEach {
        Initialize-FakeRuntimeCli
        Initialize-FakeDocker
        Remove-Item -LiteralPath (Join-Path $TestDrive 'docker-called.txt') -Force -ErrorAction SilentlyContinue
    }

    It 'does not call Docker in native mode' {
        $result = Invoke-LauncherRuntimeScenario -RuntimeMode 'native'

        $result.ExitCode | Should Be 0
        (Test-Path (Join-Path $TestDrive 'docker-called.txt')) | Should Be $false
    }

    It 'calls Docker only in Docker mode' {
        $result = Invoke-LauncherRuntimeScenario -RuntimeMode 'docker'

        $result.ExitCode | Should Be 0
        (Test-Path (Join-Path $TestDrive 'docker-called.txt')) | Should Be $true
    }

    It 'keeps NoDocker as an explicit override' {
        $result = Invoke-LauncherRuntimeScenario -RuntimeMode 'docker' -NoDocker

        $result.ExitCode | Should Be 0
        (Test-Path (Join-Path $TestDrive 'docker-called.txt')) | Should Be $false
    }
}
