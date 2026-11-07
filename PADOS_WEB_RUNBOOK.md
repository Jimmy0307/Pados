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

The local console (`/operator.html`) starts in Corpus Operator. Use the `Control Center` surface switch
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

## Request deadlines and stale actions

Every browser request goes through `web/src/http.ts` (OP-007). Each request carries an `X-Request-ID` (`web-…`), and the server returns the same request id in its `pados.error.v1` envelope. A read has a 15 s deadline, covering the response body too; it is sent as `X-Request-Deadline-Ms`, and the server honours it as a parent budget. A write (upload, scan, ingest, loop step, approval) has no client deadline and sends no budget: aborting it in the browser would not stop the server, and a budget would cut short model calls and ingestion. The operator alert shows the error code, a message and the request id; use the request id to find the matching server log lines and trace fields.

| Code shown | Meaning | What to do |
| --- | --- | --- |
| `REQUEST_TIMEOUT` | A read did not answer before its deadline. It was retried once. Nothing was written. | Check `/health/ready`; retry once the dependency is healthy. |
| `WRITE_OUTCOME_UNKNOWN` | A write lost the network (or exceeded a deadline a caller set explicitly). It may or may not have been applied, and it is never retried automatically. | Refresh the view and check whether the change is there before acting again. Idempotent endpoints accept the same key again and refuse a different payload under the same key. |
| `SESSION_STALE` | The server refused the session (`REMOTE_AUTH_INVALID`, `SESSION_UNAUTHENTICATED`, step-up required, …). | Sign in again; no action was taken. |
| `NETWORK_ERROR` | A read could not reach the server. | Check that the backend is running. |
| any server code | The server's own `pados.error.v1` code, with its `retryable` and `write_occurred` fields preserved. | Follow `docs/operations/CONTEXT_AND_ERROR_RUNBOOK.md` for that code. |

Only safe reads (`GET`, `HEAD`, `OPTIONS`) are retried, once, and only on a client timeout, a network failure, or a server envelope marked `retryable`. An identical write already in flight (for example a double click) returns the same pending request instead of a second send; uploads are never merged. These are client conveniences only: approval, idempotency and receipt authority stay on the server.

## Stop behavior

Press `Ctrl+C` in the launcher window. The script stops the frontend child and backend child it started. PostgreSQL is never stopped or modified.
