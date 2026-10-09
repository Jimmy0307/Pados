[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $MyInvocation.MyCommand.Path),
    [switch]$NoDocker
)

$ErrorActionPreference = "Stop"

function Get-PadosRuntimeMode {
    if ($env:PADOS_RUNTIME_MODE) {
        return $env:PADOS_RUNTIME_MODE.ToLowerInvariant()
    }

    $envFile = Join-Path $ProjectRoot ".env"
    if (Test-Path -LiteralPath $envFile -PathType Leaf) {
        $runtimeLine = Get-Content -LiteralPath $envFile -Encoding UTF8 |
            Where-Object { $_ -match '^\s*PADOS_RUNTIME_MODE\s*=' } |
            Select-Object -First 1
        if ($runtimeLine -match '^\s*PADOS_RUNTIME_MODE\s*=\s*(?<mode>[^#\s]+)') {
            return $Matches.mode.ToLowerInvariant()
        }
    }

    return "native"
}

function Resolve-PadosCli {
    $candidates = @(
        (Join-Path $ProjectRoot ".venv\Scripts\pados.exe"),
        (Join-Path $ProjectRoot ".venv\Scripts\pados.cmd"),
        (Join-Path $ProjectRoot "pados.exe"),
        (Join-Path $ProjectRoot "pados.cmd")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    foreach ($commandName in @("pados.exe", "pados.cmd", "pados")) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($command) {
            return $command.Source
        }
    }

    throw "PADOS_CLI_NOT_FOUND"
}

function ConvertFrom-PadosCommandText {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$CommandText
    )

    $arguments = [System.Collections.Generic.List[string]]::new()
    $current = [System.Text.StringBuilder]::new()
    $quote = [char]0
    $argumentStarted = $false
    $index = 0

    while ($index -lt $CommandText.Length) {
        $character = $CommandText[$index]

        if ($quote -eq [char]0 -and [char]::IsWhiteSpace($character)) {
            if ($argumentStarted) {
                [void]$arguments.Add($current.ToString())
                [void]$current.Clear()
                $argumentStarted = $false
            }
            $index++
            continue
        }

        if ($character -eq "'" -or $character -eq '"') {
            if ($quote -eq [char]0) {
                $quote = $character
                $argumentStarted = $true
            } elseif ($quote -eq $character) {
                $quote = [char]0
            } else {
                [void]$current.Append($character)
            }
            $index++
            continue
        }

        if ($character -eq '\' -and $quote -ne [char]0 -and
            $index + 1 -lt $CommandText.Length -and $CommandText[$index + 1] -eq $quote) {
            [void]$current.Append($CommandText[$index + 1])
            $argumentStarted = $true
            $index += 2
            continue
        }

        [void]$current.Append($character)
        $argumentStarted = $true
        $index++
    }

    if ($quote -ne [char]0) {
        throw "Unterminated quoted argument."
    }

    if ($argumentStarted) {
        [void]$arguments.Add($current.ToString())
    }

    return $arguments.ToArray()
}

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$RuntimeMode = Get-PadosRuntimeMode

if (-not $NoDocker -and $RuntimeMode -eq "docker") {
    $DockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $DockerCommand) {
        throw "DOCKER_RUNTIME_REQUESTED_BUT_DOCKER_NOT_FOUND"
    }
    & $DockerCommand.Source --context default compose up -d
    if ($LASTEXITCODE -ne 0) {
        throw "DOCKER_RUNTIME_START_FAILED"
    }
}

$PadosCli = Resolve-PadosCli
$HelpOutput = @(& $PadosCli --help 2>&1)
$HelpExitCode = $LASTEXITCODE
if ($HelpExitCode -eq 0) {
    Write-Host "[ OK ] PADOS CLI help check passed."
} else {
    Write-Host "[WARN] PADOS CLI was found, but --help returned exit code $HelpExitCode."
}

Write-Host "PADOS Shell ready. Type /exit to quit."
while ($null -ne ($InputLine = [Console]::ReadLine())) {
    $CommandText = $InputLine.Trim()
    if ([string]::IsNullOrWhiteSpace($CommandText)) {
        continue
    }
    if ($CommandText.StartsWith("/")) {
        $LauncherCommand = $CommandText.ToLowerInvariant()
        if ($LauncherCommand -in @("/exit", "/quit")) {
            break
        }
        if ($LauncherCommand -eq "/help") {
            $HelpOutput | ForEach-Object { Write-Host ([string]$_) }
            continue
        }
        if ($LauncherCommand -eq "/doctor") {
            $CommandText = "doctor"
        } elseif ($LauncherCommand -eq "/web") {
            & (Join-Path $ProjectRoot "Start-PADOS-Web.ps1")
            continue
        } else {
            Write-Host "[WARN] Unknown launcher command: $CommandText"
            continue
        }
    }

    try {
        $CommandArguments = @(ConvertFrom-PadosCommandText -CommandText $CommandText)
    } catch {
        Write-Host "[WARN] Invalid command syntax: $($_.Exception.Message)"
        continue
    }
    $PreviousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $CommandOutput = @(& $PadosCli @CommandArguments 2>&1)
        $CommandExitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $PreviousErrorActionPreference
    }
    $CommandOutput | ForEach-Object { Write-Host ([string]$_) }
    if ($CommandExitCode -ne 0) {
        Write-Host "[WARN] PADOS command returned exit code $CommandExitCode."
    }
}

exit 0
