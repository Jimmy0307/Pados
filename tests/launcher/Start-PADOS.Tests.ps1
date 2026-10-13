$ErrorActionPreference = 'Stop'

$launcherPath = Join-Path $PSScriptRoot '..\..\Start-PADOS.ps1'

function Initialize-FakePadosCli {
    $fakeCli = Join-Path $TestDrive 'pados.cmd'
    @'
@echo off
if /I "%~1"=="--help" (
  echo FAKE_HELP_OUTPUT
  echo Commands: doctor memory context worker loop
  exit /b 0
)
if /I "%~1"=="doctor" (
  echo DOCTOR_STDOUT
  echo DOCTOR_STDERR 1>&2
  exit /b 1
)
if /I "%~1"=="/doctor" (
  echo WRONG_SLASH_DOCTOR
  exit /b 9
)
if /I "%~1"=="/docker" (
  echo FORWARDED_UNKNOWN_SLASH
  exit /b 9
)
if /I "%~1"=="context" (
  echo ARG1=%~1
  echo ARG2=%~2
  echo ARG3=%~3
  echo ARG4=%~4
  echo ARG5=%~5
  echo ARG6=%~6
  echo ARG7=%~7
  echo ARG8=%~8
  echo ARG9=%~9
  shift
  shift
  shift
  shift
  shift
  shift
  shift
  shift
  shift
  echo ARG10=%~1
  echo ARG11=%~2
  echo ARG12=%~3
  exit /b 0
)
if /I "%~1"=="memory" (
  echo ARG1=%~1
  echo ARG2=%~2
  echo ARG3=%~3
  echo ARG4=%~4
  echo ARG5=%~5
  echo ARG6=%~6
  echo ARG7=%~7
  echo ARG8=%~8
  echo ARG9=%~9
  shift
  shift
  shift
  shift
  shift
  shift
  shift
  shift
  shift
  echo ARG10=%~1
  echo ARG11=%~2
  echo ARG12=%~3
  exit /b 0
)
echo ARGS:%*
exit /b 0
'@ | Set-Content -LiteralPath $fakeCli -Encoding ASCII
}

function Invoke-LauncherScenario {
    param(
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string[]]$InputLines
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $outputLines = @(
            $InputLines |
                & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $launcherPath `
                    -ProjectRoot $TestDrive -NoDocker 2>&1 |
                ForEach-Object { [string]$_ }
        )
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $exitCode = [int]$LASTEXITCODE

    [pscustomobject]@{
        ExitCode = $exitCode
        Output   = [string]($outputLines -join [Environment]::NewLine)
    }
}

Describe 'Start-PADOS launcher regressions' {
    BeforeEach {
        Initialize-FakePadosCli
    }

    It 'treats startup help exit code 0 as success' {
        $result = Invoke-LauncherScenario -InputLines @('/exit')

        $result.Output | Should Match '\[ OK \] PADOS CLI help check passed\.'
        $result.Output | Should Not Match 'CLI was found, but --help returned exit code'
    }

    It 'does not mix stdout into the exit-code result' {
        $result = Invoke-LauncherScenario -InputLines @('doctor', '/exit')

        $result.Output | Should Match 'DOCTOR_STDOUT'
        $result.Output | Should Match 'DOCTOR_STDERR'
        $result.Output | Should Match '\[WARN\] PADOS command returned exit code 1\.'
        $result.Output | Should Not Match 'PADOS command returned exit code.*DOCTOR_STDOUT'
    }

    It 'dispatches slash doctor to the doctor CLI command' {
        $result = Invoke-LauncherScenario -InputLines @('  /DoCtOr  ', '/exit')

        $result.Output | Should Match 'DOCTOR_STDOUT'
        $result.Output | Should Not Match 'WRONG_SLASH_DOCTOR'
    }

    It 'rejects unknown slash commands locally' {
        $result = Invoke-LauncherScenario -InputLines @('/docker', '/exit')

        $result.Output | Should Match '\[WARN\] Unknown launcher command: /docker'
    }

    It 'does not forward unknown slash commands to pados.exe' {
        $result = Invoke-LauncherScenario -InputLines @('/docker', '/exit')

        $result.Output | Should Not Match 'FORWARDED_UNKNOWN_SLASH'
    }

    It 'forwards ordinary doctor input normally' {
        $result = Invoke-LauncherScenario -InputLines @('  doctor  ', '/exit')

        $result.Output | Should Match 'DOCTOR_STDOUT'
        $result.Output | Should Not Match 'WRONG_SLASH_DOCTOR'
    }

    It 'prints only the integer exit code in a failed-command warning' {
        $result = Invoke-LauncherScenario -InputLines @('doctor', '/exit')

        $result.Output | Should Match '\[WARN\] PADOS command returned exit code 1\.'
        $result.Output | Should Not Match '\[WARN\] PADOS command returned exit code .*DOCTOR_'
    }

    It 'does not crash on empty input' {
        $result = Invoke-LauncherScenario -InputLines @('', '/exit')

        $result.ExitCode | Should Be 0
        $result.Output | Should Not Match 'null-valued expression'
        $result.Output | Should Not Match 'ARGS:'
    }

    It 'preserves a quoted query containing spaces as one argument' {
        $result = Invoke-LauncherScenario -InputLines @(
            'context assemble --query "PADOS native runtime validation" --purpose project-operations',
            '/exit'
        )

        $result.Output | Should Match 'ARG4=PADOS native runtime validation'
        $result.Output | Should Match 'ARG6=project-operations'
    }

    It 'preserves quoted title and summary values containing spaces' {
        $result = Invoke-LauncherScenario -InputLines @(
            'memory fact-add --project-id 00000000-0000-0000-0000-000000000000 --title "Title with spaces" --summary "Summary with spaces" --source-uri obs:launcher',
            '/exit'
        )

        $result.Output | Should Match 'ARG6=Title with spaces'
        $result.Output | Should Match 'ARG8=Summary with spaces'
    }

    It 'passes shell-like text literally without arbitrary PowerShell execution' {
        $marker = Join-Path $TestDrive 'unexpected-execution.txt'
        $injection = 'memory search --query "Powershell -NoProfile -Command Get-Date"'
        $result = Invoke-LauncherScenario -InputLines @($injection, '/exit')

        $result.Output | Should Match 'ARG4=Powershell -NoProfile -Command Get-Date'
        (Test-Path -LiteralPath $marker) | Should Be $false
        (Get-Content -LiteralPath $launcherPath -Raw) | Should Not Match 'Invoke-Expression'
    }

    It 'does not advertise unavailable gate or task commands' {
        $result = Invoke-LauncherScenario -InputLines @('/exit')

        $result.Output | Should Not Match '/gate'
        $result.Output | Should Not Match 'task --help'
        $result.Output | Should Not Match 'task create --help'
    }
}
