param(
  [ValidateSet("native", "wsl", "docker")]
  [string]$RuntimeMode = $(if ($env:PADOS_RUNTIME_MODE) { $env:PADOS_RUNTIME_MODE } else { "native" })
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\.."
$LogDir = Join-Path $Root "runtime\logs"
$PidFile = Join-Path $Root "runtime\pids\pados-api.pid"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "stop.log"
$HostValue = if ($env:PADOS_API_HOST) { $env:PADOS_API_HOST } else { "127.0.0.1" }
$PortValue = if ($env:PADOS_API_PORT) { [int]$env:PADOS_API_PORT } else { 8088 }

Push-Location $Root
try {
  if ($RuntimeMode -eq "docker") {
    cmd /c "docker --context default compose stop > ""$Log"" 2>&1"
    exit $LASTEXITCODE
  }

  if ($RuntimeMode -eq "wsl") {
    "PADOS_RUNTIME_MODE=wsl selected. Stop the API in the WSL environment; PostgreSQL remains externally managed." | Out-File -FilePath $Log -Encoding utf8
    exit 2
  }

  $PidValue = $null
  if (Test-Path $PidFile) {
    $PidValue = [int](Get-Content -Raw $PidFile)
    $Process = Get-Process -Id $PidValue -ErrorAction SilentlyContinue
    if ($Process) {
      Stop-Process -Id $PidValue -ErrorAction Stop
      "Stopped PADOS API pid=$PidValue. PostgreSQL was not modified." | Out-File -FilePath $Log -Encoding utf8
    } else {
      "PADOS API pid=$PidValue was not running. PostgreSQL was not modified." | Out-File -FilePath $Log -Encoding utf8
    }
  } else {
    "No PADOS API PID file found. Checking listener on $HostValue`:$PortValue. PostgreSQL was not modified." | Out-File -FilePath $Log -Encoding utf8
  }
  $Listeners = Get-NetTCPConnection -LocalAddress $HostValue -LocalPort $PortValue -State Listen -ErrorAction SilentlyContinue
  foreach ($Listener in $Listeners) {
    if ($null -eq $PidValue -or $Listener.OwningProcess -ne $PidValue) {
      Stop-Process -Id $Listener.OwningProcess -ErrorAction SilentlyContinue
      "Stopped PADOS API listener pid=$($Listener.OwningProcess) on $HostValue`:$PortValue. PostgreSQL was not modified." | Out-File -FilePath $Log -Encoding utf8 -Append
    }
  }
  Remove-Item -LiteralPath $PidFile -Force -ErrorAction SilentlyContinue
  exit 0
}
finally {
  Pop-Location
}
