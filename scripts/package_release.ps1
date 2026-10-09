param(
  [string]$OutputPath = "outputs\pados_release_p6_10_20260711.zip"
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\.."
. (Join-Path $PSScriptRoot "lib\Initialize-PadosRuntime.ps1")
$LogDir = Join-Path $Root "work\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir "p6_10_package_release.log"

Push-Location $Root
try {
  $Python = Initialize-PadosRuntime -ProjectRoot $Root
  Assert-PadosImportPath -Python $Python -ProjectRoot $Root
  & $Python -m pados.release.package --root . --output $OutputPath *> $Log
  exit $LASTEXITCODE
}
finally {
  Pop-Location
}
