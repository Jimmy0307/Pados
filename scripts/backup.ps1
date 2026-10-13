param(
  [string]$OutputDir = "runtime\artifacts\backups",
  [ValidateSet("native", "wsl", "docker")]
  [string]$RuntimeMode = $(if ($env:PADOS_RUNTIME_MODE) { $env:PADOS_RUNTIME_MODE } else { "native" })
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\.."
. (Join-Path $PSScriptRoot "lib\Initialize-PadosRuntime.ps1")
$Python = Initialize-PadosRuntime -ProjectRoot $Root
Assert-PadosImportPath -Python $Python -ProjectRoot $Root
$LogDir = Join-Path $Root "runtime\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $Root $OutputDir) | Out-Null
$Timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$Backup = Join-Path $Root "$OutputDir\pados_$Timestamp.dump"
$Log = Join-Path $LogDir "backup.log"

$DbHost = if ($env:PADOS_DB_HOST) { $env:PADOS_DB_HOST } else { "127.0.0.1" }
$DbPort = if ($env:PADOS_DB_PORT) { $env:PADOS_DB_PORT } else { "5432" }
$DbName = if ($env:PADOS_DB_NAME) { $env:PADOS_DB_NAME } else { "pados_p6_7_validation" }
$DbUser = if ($env:PADOS_DB_USER) { $env:PADOS_DB_USER } else { "pados" }
$DbPassword = if ($env:PADOS_DB_PASSWORD) { $env:PADOS_DB_PASSWORD } else { "pados_local_password" }

Push-Location $Root
try {
  if ($RuntimeMode -eq "docker") {
    cmd /c "docker --context default compose exec -T postgres pg_dump -U pados -d pados -Fc -f /tmp/pados.dump > ""$Log"" 2>&1"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    cmd /c "docker --context default cp pados-postgres:/tmp/pados.dump ""$Backup"" >> ""$Log"" 2>&1"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Output $Backup | Tee-Object -FilePath (Join-Path $LogDir "last_backup_path.txt")
    exit 0
  }

  if ($RuntimeMode -eq "wsl") {
    "PADOS_RUNTIME_MODE=wsl selected. Run pg_dump inside WSL using the same database settings." | Out-File -FilePath $Log -Encoding utf8
    exit 2
  }

  $PgDump = Get-Command "pg_dump" -ErrorAction Stop
  $env:PGPASSWORD = $DbPassword
  & $PgDump.Source --host $DbHost --port $DbPort --username $DbUser --dbname $DbName --format custom --file $Backup > $Log 2>&1
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  Write-Output $Backup | Tee-Object -FilePath (Join-Path $LogDir "last_backup_path.txt")
  exit 0
}
finally {
  Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
  Pop-Location
}
