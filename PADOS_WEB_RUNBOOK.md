# PADOS Web Control Center Runbook

## Prerequisites

- Windows PowerShell
- `<PADOS_ROOT>\.venv\Scripts\python.exe`
- Node.js and npm
- PostgreSQL available through the existing local PADOS settings

## First-time frontend setup

```powershell
Set-Location <PADOS_ROOT>\web
npm install
```

## One-command startup

```powershell
Set-Location <PADOS_ROOT>
.\Start-PADOS-Web.ps1
```

The launcher checks the Python runtime, npm dependencies, `pados doctor`, localhost ports and backend health. It selects the next free port when 8088 or 5173 is occupied, opens the browser, and cleans up only the child processes it started when stopped.

Use `-NoBrowser` for automation. Override ports with `-BackendPort` and `-FrontendPort`.

## URLs

- Frontend: printed by the launcher, normally `http://127.0.0.1:5173`
- Backend: printed by the launcher, normally `http://127.0.0.1:8088`
- Existing API health: `/health`
- Web API health: `/api/health`

The browser starts in Corpus Operator. Use the `Control Center` surface switch
to open the read-only Dashboard, Projects, Memory, Database, Runs & Events, and
System views. Corpus authorization and ingest remain available only in Corpus
Operator.

## Existing launcher integration

Inside `Start-PADOS.ps1`, enter `/web` to start the control center. Existing `/doctor` and ordinary CLI commands remain routed as before.

## Useful checks

```powershell
Set-Location <PADOS_ROOT>
.\.venv\Scripts\python.exe -m pados.cli.main doctor --json
.\.venv\Scripts\python.exe -m pytest tests\unit\test_web_api.py -q
Set-Location .\web
npm run typecheck
npm run lint
npm test
npm run build
```

## Stop behavior

Press `Ctrl+C` in the launcher window. The script stops the frontend child and backend child it started. PostgreSQL is never stopped or modified.
