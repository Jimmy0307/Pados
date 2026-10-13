param(
  [ValidateSet("native", "wsl", "docker")]
  [string]$RuntimeMode = $(if ($env:PADOS_RUNTIME_MODE) { $env:PADOS_RUNTIME_MODE } else { "native" })
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\.."
. (Join-Path $PSScriptRoot "lib\Initialize-PadosRuntime.ps1")
$LogDir = Join-Path $Root "runtime\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "healthcheck.log"

Push-Location $Root
try {
  $env:PADOS_RUNTIME_MODE = $RuntimeMode
  $HostValue = if ($env:PADOS_API_HOST) { $env:PADOS_API_HOST } else { "127.0.0.1" }
  $PortValue = if ($env:PADOS_API_PORT) { $env:PADOS_API_PORT } else { "8088" }
  $HealthUrl = "http://$HostValue`:$PortValue/health"
  $Python = Initialize-PadosRuntime -ProjectRoot $Root
  Assert-PadosImportPath -Python $Python -ProjectRoot $Root

  "runtime_mode=$RuntimeMode" | Out-File -FilePath $Log -Encoding utf8
  if ($RuntimeMode -eq "wsl") {
    "PADOS_RUNTIME_MODE=wsl selected. Run this health contract inside WSL with matching PostgreSQL settings." | Tee-Object -FilePath $Log -Append
    exit 2
  }

  $Health = $null
  for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
    try {
      $Health = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 3
      break
    } catch {
      Start-Sleep -Milliseconds 500
    }
  }
  if ($null -eq $Health) {
    "api_health=error unable_to_connect" | Tee-Object -FilePath $Log -Append
    exit 1
  }
  "api_health=$($Health.status)" | Tee-Object -FilePath $Log -Append

  cmd /c """$Python"" -m pados.cli.main doctor --json >> ""$Log"" 2>&1"
  exit $LASTEXITCODE
}
finally {
  Pop-Location
}
