param()

$ErrorActionPreference = "Stop"
$Root = Resolve-Path "$PSScriptRoot\.."
Push-Location $Root
try {
  python -m venv .venv
  & ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
  & ".\.venv\Scripts\python.exe" -m pip install -e .[dev]
  & ".\.venv\Scripts\python.exe" -m pip freeze | Set-Content -Encoding UTF8 requirements.lock
}
finally {
  Pop-Location
}

