$ErrorActionPreference = "Stop"

function Get-PadosProjectRoot {
  return (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
}

function Initialize-PadosRuntime {
  param([string]$ProjectRoot = (Get-PadosProjectRoot))

  $Root = (Resolve-Path -LiteralPath $ProjectRoot).Path
  $Source = (Resolve-Path -LiteralPath (Join-Path $Root "src")).Path
  $Separator = [System.IO.Path]::PathSeparator
  $Entries = @($Source)
  if ($env:PYTHONPATH) {
    $Entries += $env:PYTHONPATH -split [regex]::Escape([string]$Separator)
  }

  $Seen = [System.Collections.Generic.HashSet[string]]::new(
    [System.StringComparer]::OrdinalIgnoreCase
  )
  $Normalized = foreach ($Entry in $Entries) {
    if ([string]::IsNullOrWhiteSpace($Entry)) { continue }
    $Candidate = $Entry.Trim()
    try { $Candidate = [System.IO.Path]::GetFullPath($Candidate) } catch { continue }
    if ($Seen.Add($Candidate)) { $Candidate }
  }
  $env:PYTHONPATH = $Normalized -join $Separator

  $Python = Join-Path $Root ".venv\Scripts\python.exe"
  if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "PADOS_PROJECT_PYTHON_NOT_FOUND"
  }
  return $Python
}

function Assert-PadosImportPath {
  param(
    [Parameter(Mandatory = $true)][string]$Python,
    [string]$ProjectRoot = (Get-PadosProjectRoot)
  )

  $Expected = Join-Path ((Resolve-Path -LiteralPath $ProjectRoot).Path) "src\pados"
  & $Python -c "from pathlib import Path; import pados; actual=Path(pados.__file__).resolve(); expected=Path(r'$Expected').resolve(); raise SystemExit(0 if actual == expected / '__init__.py' else 86)"
  if ($LASTEXITCODE -ne 0) {
    throw "WORKTREE_IMPORT_PATH_MISMATCH"
  }
}
