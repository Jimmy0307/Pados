param(
  [Parameter(Mandatory = $true)]
  [string]$BackupPath,
  [string]$TargetDatabase = "pados_restore_smoke",
  [switch]$AllowOverwriteProduction,
  [switch]$RecreateTarget,
  [ValidateSet("native", "wsl", "docker")]
  [string]$RuntimeMode = $(if ($env:PADOS_RUNTIME_MODE) { $env:PADOS_RUNTIME_MODE } else { "native" })
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\.."
. (Join-Path $PSScriptRoot "lib\Initialize-PadosRuntime.ps1")
$Python = Initialize-PadosRuntime -ProjectRoot $Root
Assert-PadosImportPath -Python $Python -ProjectRoot $Root
$ResolvedBackup = Resolve-Path $BackupPath
$LogDir = Join-Path $Root "runtime\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "restore.log"
$RestoreList = Join-Path $LogDir "restore.filtered.list"

$DbHost = if ($env:PADOS_DB_HOST) { $env:PADOS_DB_HOST } else { "127.0.0.1" }
$DbPort = if ($env:PADOS_DB_PORT) { $env:PADOS_DB_PORT } else { "5432" }
$DbUser = if ($env:PADOS_DB_USER) { $env:PADOS_DB_USER } else { "pados" }
$DbPassword = if ($env:PADOS_DB_PASSWORD) { $env:PADOS_DB_PASSWORD } else { "pados_local_password" }

if ($TargetDatabase -eq "pados" -and -not $AllowOverwriteProduction) {
  throw "Refusing to restore over production database without -AllowOverwriteProduction."
}

Push-Location $Root
try {
  if ($RuntimeMode -eq "docker") {
    cmd /c "docker --context default cp ""$ResolvedBackup"" pados-postgres:/tmp/pados_restore.dump > ""$Log"" 2>&1"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    cmd /c "docker --context default compose exec -T postgres dropdb -U pados --if-exists $TargetDatabase >> ""$Log"" 2>&1"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    cmd /c "docker --context default compose exec -T postgres createdb -U pados $TargetDatabase >> ""$Log"" 2>&1"
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    cmd /c "docker --context default compose exec -T postgres pg_restore -U pados -d $TargetDatabase /tmp/pados_restore.dump >> ""$Log"" 2>&1"
    exit $LASTEXITCODE
  }

  if ($RuntimeMode -eq "wsl") {
    "PADOS_RUNTIME_MODE=wsl selected. Run dropdb/createdb/pg_restore inside WSL using the same database settings." | Out-File -FilePath $Log -Encoding utf8
    exit 2
  }

  $PgRestore = Get-Command "pg_restore" -ErrorAction Stop
  $env:PGPASSWORD = $DbPassword

  if ($RecreateTarget) {
    $DropDb = Get-Command "dropdb" -ErrorAction Stop
    $CreateDb = Get-Command "createdb" -ErrorAction Stop
    & $DropDb.Source --host $DbHost --port $DbPort --username $DbUser --if-exists $TargetDatabase > $Log 2>&1
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    & $CreateDb.Source --host $DbHost --port $DbPort --username $DbUser $TargetDatabase >> $Log 2>&1
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
  } else {
    "Using existing restore target database $TargetDatabase. PostgreSQL role and database ownership are not modified." | Out-File -FilePath $Log -Encoding utf8
  }

  & $PgRestore.Source --list $ResolvedBackup | Where-Object {
    $_ -notmatch "EXTENSION .* vector" -and
    $_ -notmatch "COMMENT .* EXTENSION vector" -and
    $_ -notmatch "EXTENSION .* pgcrypto" -and
    $_ -notmatch "COMMENT .* EXTENSION pgcrypto"
  } | Set-Content -Path $RestoreList -Encoding ascii
  & $PgRestore.Source --host $DbHost --port $DbPort --username $DbUser --dbname $TargetDatabase --use-list $RestoreList --clean --if-exists --no-owner --no-privileges $ResolvedBackup >> $Log 2>&1
  exit $LASTEXITCODE
}
finally {
  Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
  Pop-Location
}
