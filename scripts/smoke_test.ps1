param()

$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\.."
. (Join-Path $PSScriptRoot "lib\Initialize-PadosRuntime.ps1")
Push-Location $Root
try {
  $Python = Initialize-PadosRuntime -ProjectRoot $Root
  Assert-PadosImportPath -Python $Python -ProjectRoot $Root
  & ".\scripts\start.ps1"
  & $Python -m alembic upgrade head
  & $Python -m pados.cli.main doctor
  & ".\scripts\healthcheck.ps1"
  & ".\scripts\backup.ps1"
  $Backup = Get-Content -LiteralPath ".\runtime\logs\last_backup_path.txt" | Select-Object -Last 1
  & ".\scripts\restore.ps1" -BackupPath $Backup
}
finally {
  Pop-Location
}

