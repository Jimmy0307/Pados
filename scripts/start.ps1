param(
  [ValidateSet("native", "wsl", "docker")]
  [string]$RuntimeMode = $(if ($env:PADOS_RUNTIME_MODE) { $env:PADOS_RUNTIME_MODE } else { "native" })
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\.."
. (Join-Path $PSScriptRoot "lib\Initialize-PadosRuntime.ps1")
$LogDir = Join-Path $Root "runtime\logs"
$PidDir = Join-Path $Root "runtime\pids"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $PidDir | Out-Null
$Log = Join-Path $LogDir "start.log"
$ErrorLog = Join-Path $LogDir "start.error.log"
$ControlLog = Join-Path $LogDir "start.control.log"
$PidFile = Join-Path $PidDir "pados-api.pid"

function Repair-ProcessPathEnvironment {
  $PathValue = [Environment]::GetEnvironmentVariable("Path", "Process")
  $UpperPathValue = [Environment]::GetEnvironmentVariable("PATH", "Process")
  $EffectivePath = if ($PathValue) { $PathValue } else { $UpperPathValue }
  [Environment]::SetEnvironmentVariable("PATH", $null, "Process")
  if ($EffectivePath) {
    [Environment]::SetEnvironmentVariable("Path", $EffectivePath, "Process")
  }
}

Push-Location $Root
try {
  if ($RuntimeMode -eq "docker") {
    cmd /c "docker --context default compose up -d > ""$Log"" 2>&1"
    exit $LASTEXITCODE
  }

  if ($RuntimeMode -eq "wsl") {
    "PADOS_RUNTIME_MODE=wsl selected. Start the API in the WSL environment with the same PostgreSQL connectivity contract." | Out-File -FilePath $Log -Encoding utf8
    exit 2
  }

  $env:PADOS_RUNTIME_MODE = "native"
  $HostValue = if ($env:PADOS_API_HOST) { $env:PADOS_API_HOST } else { "127.0.0.1" }
  $PortValue = if ($env:PADOS_API_PORT) { $env:PADOS_API_PORT } else { "8088" }
  $Python = Initialize-PadosRuntime -ProjectRoot $Root
  Assert-PadosImportPath -Python $Python -ProjectRoot $Root
  $Arguments = @("-m", "uvicorn", "pados.api.app:app", "--host", $HostValue, "--port", $PortValue)
  Repair-ProcessPathEnvironment
  $ArgumentText = ($Arguments | ForEach-Object { '"' + ($_ -replace '"', '\"') + '"' }) -join " "
  $ChildCommand = "Set-Location -LiteralPath '$Root'; & '$Python' $ArgumentText 1>'$Log' 2>'$ErrorLog'"
  Start-Process -FilePath "powershell.exe" -ArgumentList @(
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-Command",
    $ChildCommand
  ) -WorkingDirectory $Root -WindowStyle Hidden | Out-Null
  $ListenerPid = $null
  for ($Attempt = 0; $Attempt -lt 20; $Attempt++) {
    Start-Sleep -Milliseconds 250
    $Listener = Get-NetTCPConnection -LocalAddress $HostValue -LocalPort ([int]$PortValue) -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($Listener) {
      $ListenerPid = $Listener.OwningProcess
      break
    }
  }
  if (-not $ListenerPid) {
    "PADOS API failed to open listener on $HostValue`:$PortValue" | Out-File -FilePath $ControlLog -Encoding utf8
    exit 1
  }
  $PidToRecord = $ListenerPid
  $PidToRecord | Out-File -FilePath $PidFile -Encoding ascii
  "Started PADOS API pid=$PidToRecord mode=native url=http://$HostValue`:$PortValue" | Out-File -FilePath $ControlLog -Encoding utf8
  exit 0
}
finally {
  Pop-Location
}
