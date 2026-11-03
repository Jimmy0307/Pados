# Local usage guide (single user, this computer, supervised, synthetic or non-sensitive data)

This guide follows the path that was actually walked on Windows 11 from a fresh clone, on 2026-11-03. Each step
below ran as written. Where PADOS has no command for a step, the guide says so instead of inventing one. For the
underlying commands see [`QUICKSTART.md`](QUICKSTART.md); the automated version of the whole journey is
`scripts/dogfood/local_dogfood_e2e.py`.

What this is not: a multi-user, networked or production setup; cloud or external providers; unattended agents.
Model output never becomes an accepted result by itself (see "What a result means").

## 1. Install, initialise, configure

```powershell
git clone <repository> pados; cd pados
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -c requirements.lock -e ".[dev]"
.\.venv\Scripts\pados init          # writes .env and runtime\local\control.token (keep the token private)
```

Edit `.env`: set `PADOS_DB_HOST`, `PADOS_DB_PORT` and `PADOS_DB_PASSWORD` for your PostgreSQL (16 or later, with
pgvector). `pados init` leaves `PADOS_DB_PASSWORD=CHANGE_ME` and port 5432. Create the three databases named in
`.env` (`pados_dogfood_operational`, `pados_dogfood_proof`, `pados_dogfood_corpus`), then migrate them. In
PowerShell, an environment variable is set with `$env:`:

```powershell
$env:PADOS_DB_NAME="pados_dogfood_operational"; .\.venv\Scripts\alembic upgrade 20261101_0020
$env:PADOS_DB_NAME="pados_dogfood_proof";       .\.venv\Scripts\alembic upgrade 20260730_0011
$env:PADOS_DB_NAME="pados_dogfood_proof";       .\.venv\Scripts\alembic upgrade 20260808_0013
$env:PADOS_DB_NAME="pados_dogfood_proof";       .\.venv\Scripts\alembic -c alembic_p6_19b.ini upgrade head
$env:PADOS_DB_NAME="pados_dogfood_corpus";      .\.venv\Scripts\alembic upgrade 20260730_0011
Remove-Item Env:PADOS_DB_NAME
.\.venv\Scripts\pados doctor        # "status": "ok" is the Core readiness verdict
```

Never run `alembic upgrade heads` on the proof database.

## 2. Start the service, check health, open the web console

```powershell
.\.venv\Scripts\pados serve         # API on http://127.0.0.1:8088 ; Ctrl+C stops it
```

- `http://127.0.0.1:8088/health/live` answers `ALIVE` (the process is up; nothing else is claimed).
- `http://127.0.0.1:8088/health/ready` lists each dependency. `operational_database` is required; the others,
  including `corpus_database`, are optional. `chat_provider` is `NOT_CONFIGURED` by default, which is expected.
- Web console: `cd web; npm ci; npm run dev`, then open **`http://127.0.0.1:5173/operator.html`**. That is the local
  operator console (Corpus Operator, and the Control Center tab with Mission Control and the Decision Inbox). The root
  page `http://127.0.0.1:5173/` is the **Remote console sign-in**, which needs Remote Operations configured; it links
  to the local console.

## 3. Create a project

```powershell
.\.venv\Scripts\pados project create --name demo --root-path .\runtime\local\workspace --json
```

There is no web or API route that creates a project; until one exists, the console's project list tells you to use
this command.

## 4. Run a real job (the deterministic Markdown reference check)

The control API takes a work request, an approval with a WORK_ROUTE subject, a goal, a plan and a TaskLoop. **There
is no CLI or web form for work requests and approvals yet**, so this step needs the control token
(`runtime\local\control.token`, sent as the `X-PADOS-Control-Token` header) and the JSON bodies that
`task_flow` in `scripts/dogfood/local_dogfood_e2e.py` builds. In order:

1. `POST /control/work-requests` (project, title, a TaskEnvelope body).
2. `POST /control/approvals` with a WORK_ROUTE subject naming `markdown_reference_check`. A loop cannot be created
   before the approval is approved.
3. `POST /control/approvals/{id}/approve` — your decision as the operator, with the approval's `version`.
4. `POST /control/task-goals`, `POST /control/task-plans`, `POST /control/loops`, `POST /control/loops/{id}/start`.
5. `POST /control/loops/{id}/step` until the loop is `COMPLETED`.

The job runs the read-only probe on the project's Markdown files and records its outcome and SHA-256 digests.

## 5. See progress, errors, results and evidence

```powershell
.\.venv\Scripts\pados loop status  --loop-id <loop-uuid> --json
.\.venv\Scripts\pados loop inspect --loop-id <loop-uuid> --json
.\.venv\Scripts\pados loop events  --loop-id <loop-uuid> --json
```

`GET /control/jobs` and `GET /control/jobs/{id}/artifacts` list the job and its evidence (handoff, result manifest,
stdout, stderr, each with a SHA-256). Errors use one envelope, `pados.error.v1`: a stable `code`, whether it is safe
to retry, and whether anything was written (`write_occurred`). `--json` output is one JSON document; log lines go to
standard error.

## 6. Pause, resume, cancel, restart, stop

- `POST /control/loops/{id}/pause`, `/resume`, `/cancel` (or `pados loop pause|resume|cancel --loop-id ...`).
  A step on a paused or cancelled loop is refused with `LOOP_NOT_RUNNING` and runs nothing.
- Stop the server with Ctrl+C; start it again with `pados serve`. Loops, jobs, approvals and pending results are in
  PostgreSQL and come back as they were (checked after a hard stop, too).
- A loop has a five-minute lease. If a step is refused with `LOOP_LEASE_HELD`, another owner holds it; wait for it,
  or for the lease to lapse when that owner is gone.

## 7. What a result means

| You see | It means |
|---|---|
| Loop `COMPLETED`, job `SUCCEEDED`, validation `PASSED` | **Execution evidence.** The registered command ran and its declared outcome passed. Nothing is accepted yet. |
| `pados loop submit-result-candidate` → `WAITING_APPROVAL` | A verified **ResultCandidate**, not accepted. The Decision Inbox shows it as "Verified, not accepted yet". |
| Accepted / `COMMITTED`, ScopeVersion advanced | **Governed acceptance**: only after a fresh WebAuthn step-up in the Remote console, through the single CAS. The control token cannot accept or reject a result (`RESULT_ACCEPTANCE_REQUIRES_STEP_UP`). |

Model output (local Ollama) is execution evidence at most; it is never accepted by itself.

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| `pados doctor` fails on the database | Wrong host, port or password in `.env`, or PostgreSQL is not running | Fix `.env`; `pados doctor` names the failing check |
| `pados serve` exits with `DEFAULT_CONTROL_TOKEN_REFUSED` | `.env` holds the public default token | Run `pados init --force` in a fresh checkout, or set a unique token hash |
| Browser shows a sign-in asking for an actor and access proof | That is the Remote console | Open `/operator.html` instead |
| Project list empty | No project exists yet | `pados project create --name <name> --root-path <folder>` |
| `403` on `/control/...` | Missing or wrong `X-PADOS-Control-Token` | Send the token from `runtime\local\control.token` |
| `409 LOOP_NOT_RUNNING` | The loop is paused, finished or cancelled | Resume a paused loop; create a new loop otherwise |
| `403 RESULT_ACCEPTANCE_REQUIRES_STEP_UP` | A result acceptance was decided through the control API | Decide it in the Remote console with a WebAuthn step-up |
| `/health/ready` `DEGRADED`, `corpus_database` not OK | The Corpus database is down or not migrated | The core loop still works; start it or migrate it |
| `chat_provider` `NOT_CONFIGURED`, cognitive actions report `COGNITIVE_PROVIDER_NOT_CONFIGURED` | No chat model is configured (default) | Expected; a local model runs only under a measured binding with an explicit authorization |
