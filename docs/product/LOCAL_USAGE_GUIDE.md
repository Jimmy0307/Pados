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
  operator console (Corpus Operator, and the Control Center tab with Mission Control, the Decision Inbox, Approvals
  and New work request). The root page `http://127.0.0.1:5173/` is the **Remote console sign-in**, which needs Remote
  Operations configured; it links to the local console. In a build served under a sub-path (for example `/Pados/`),
  every page link stays under that sub-path.
- Or start both with one command on Windows: `.\Start-PADOS-Web.ps1`. It runs `pados doctor`, then `pados serve`
  (with the same refusals: a non-loopback host or the shipped default token stops it, and the launcher prints
  `BACKEND_START_REFUSED` with the reason), waits for `/health/live`, starts the web console and opens
  `/operator.html`.

## 3. Create a project

```powershell
.\.venv\Scripts\pados project create --name demo --root-path .\runtime\local\workspace --json
```

There is no web or API route that creates a project; until one exists, the console's project list tells you to use
this command.

## 4. Run a real job (the deterministic Markdown reference check)

The control API takes a work request, an approval with a WORK_ROUTE subject, a goal, a plan and a TaskLoop. All of
them need the control token (`runtime\local\control.token`). In order:

1. **Work request (web):** Control Center → **New work request**. Paste the control token into the masked field
   (it stays in that page's memory only; reloading forgets it), choose the project, give a title, the capability the
   work needs (for this probe, **Run a static check**) and what should be done, **Create work request**. Or
   `POST /control/work-requests` with `requested_capability` (for example `RUN_STATIC_ANALYSIS`). Naming the
   capability makes the server record the request as a task: its body then carries a hashed TaskEnvelope (your text,
   one pending goal that requires that capability, the project's classification and the current ScopeVersion). A plan
   step bound to that goal (`product_goal_id` = the envelope's goal id) is what a ResultCandidate needs later. The
   capability grants nothing; the approval still decides what may run. Without it the request is stored as plain
   text, and a plan bound to a product goal is refused (`TASK_ENVELOPE_REQUIRED_FOR_RUNTIME_BINDING`). Capture is
   refused when no ScopeVersion can be read (`TASK_ENVELOPE_SCOPE_VERSION_UNAVAILABLE`) or when the title and text
   exceed 2000 characters (`TASK_INTENT_TOO_LONG`). The envelope keeps the ScopeVersion it was captured at: if an
   accepted result moves the scope before you plan, the plan is refused as stale; create a new work request.
2. **Approval request (API only):** `POST /control/approvals` with a WORK_ROUTE subject naming
   `markdown_reference_check` (the JSON body `task_flow` in `scripts/dogfood/local_dogfood_e2e.py` builds). There is
   no form for this yet: writing the subject (capability, command, worker type, maximum risk) is a product decision.
3. **Your decision (web):** Control Center → **Approvals** (or the inbox's **Review**). The panel shows what the
   approval authorizes: request, worker type, each capability and command with its maximum risk, and the version.
   **Approve**, **Reject** or **Request revision** asks you to confirm that exact approval before anything is sent.
   Or `POST /control/approvals/{id}/approve` with the approval's `version`. A loop cannot be created before this.
4. **Goal, plan, loop (API only):** `POST /control/task-goals`, `POST /control/task-plans`, `POST /control/loops`,
   `POST /control/loops/{id}/start`, then `POST /control/loops/{id}/step` until the loop is `COMPLETED`, or
   `pados loop step <id>`.

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
| `pados loop submit-result-candidate` → `WAITING_APPROVAL` | A verified **ResultCandidate**, not accepted. The Decision Inbox shows it as "Verified, not accepted yet", with **Open in Remote console**: that link opens the Remote console's Actions view, whose sign-in lists what you need (a Remote sign-in, and a WebAuthn device enrolled for your actor). The Control Center has no decide buttons for it. |
| Accepted / `COMMITTED`, ScopeVersion advanced | **Governed acceptance**: only after a fresh WebAuthn step-up in the Remote console, through the single CAS. The control token cannot accept or reject a result (`RESULT_ACCEPTANCE_REQUIRES_STEP_UP`). |

Model output (local Ollama) is execution evidence at most; it is never accepted by itself.

## Troubleshooting

| Symptom | Cause | What to do |
|---|---|---|
| `pados doctor` fails on the database | Wrong host, port or password in `.env`, or PostgreSQL is not running | Fix `.env`; `pados doctor` names the failing check |
| `pados serve` exits with `DEFAULT_CONTROL_TOKEN_REFUSED` (the launcher: `BACKEND_START_REFUSED ... DEFAULT_CONTROL_TOKEN_REFUSED`) | `.env` holds the public default token | Run `pados init --force` in a fresh checkout, or set a unique token hash |
| `pados init` prints `exists: ... (use --force to overwrite)` | An `.env` is already there; init never replaces a working token silently | Keep it, or `pados init --force` for a new token (then use the new one everywhere) |
| `PADOS_LOG_LEVEL_INVALID: the value is not one of ...` on standard error | `PADOS_LOG_LEVEL` is not DEBUG, INFO, WARNING, ERROR or CRITICAL | Commands still run and log at INFO; fix the value. `pados doctor --json` reports it under `settings.log_level` |
| `WORKTREE_IMPORT_PATH_MISMATCH` from `pados doctor` | Run from another directory than the checkout the CLI is installed from | `cd` to the checkout the message names |
| Control Center: `Not recorded: ... version` or `approval is not pending` | Someone decided or changed the approval since you opened it | Reopen it from Approvals; it shows the current version and status |
| Browser shows a sign-in asking for an actor and access proof | That is the Remote console | Open `/operator.html` instead |
| Project list empty | No project exists yet | `pados project create --name <name> --root-path <folder>` |
| `403` on `/control/...` | Missing or wrong `X-PADOS-Control-Token` | Send the token from `runtime\local\control.token` |
| `409 LOOP_NOT_RUNNING` | The loop is paused, finished or cancelled | Resume a paused loop; create a new loop otherwise |
| `403 RESULT_ACCEPTANCE_REQUIRES_STEP_UP` | A result acceptance was decided through the control API | Decide it in the Remote console with a WebAuthn step-up |
| Remote step-up decision fails after your device verified it; the same proof is then `APPROVAL_PROOF_REPLAYED` | The proof is consumed in its own transaction before the decision is recorded; a failure in between spends it and decides nothing | Run a new step-up (approve or reject again); nothing was accepted |
| `/health/ready` `DEGRADED`, `corpus_database` not OK | The Corpus database is down or not migrated | The core loop still works; start it or migrate it |
| `chat_provider` `NOT_CONFIGURED`, cognitive actions report `COGNITIVE_PROVIDER_NOT_CONFIGURED` | No chat model is configured (default) | Expected; a local model runs only under a measured binding with an explicit authorization |
