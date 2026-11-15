# Work Map and Resources V1 (Control Center)

Status: **IMPLEMENTED (read-only projections)**. Pages: 工作心智圖 (Work Map) and 資源與儲存 (Resources) in the Local
Control Center. Backend: `src/pados/control/work_projection.py` (`project_execution`),
`src/pados/control/resource_metrics.py`, `src/pados/control/resource_summary.py`, `src/pados/control/cleanup_plan.py`,
routes in `src/pados/control/product_routes.py`. Web: `web/src/work-map*.ts(x)`, `web/src/resources*.ts(x)`.

Both routes sit behind the single `control_auth` dependency (control token, or a paired session with CSRF and Origin).
Both are **PROJECTION ONLY**: they read existing records and measure; they write, delete, schedule, route, approve and
accept nothing. No migration, no new table, no new runtime dependency. When the session scope allowlist
(`src/pados/identity/scopes.py`) is integrated, both GET routes must be added there as allowed for sessions.

## Work Map (`GET /control/projects/{project_id}/work-map`)

The response is the existing project work projection (`project_work`: work requests, approvals, task goals, plans with
steps, TaskLoops, ResultCandidates and their decisions, audit events) plus `execution`: the project's TaskLoop
iterations (with the step each one executed), their worker jobs (status, timings, exit and validation codes) and the
recorded job artifacts (type, SHA-256, time; never a path). Bounded: 50 rows per work list, 200 iterations and jobs,
400 artifacts (`truncated` says when a bound was hit).

The browser builds the tree (`web/src/work-map.ts`) on every read; nothing is stored. Node kinds follow the work-graph
vocabulary (`pados.work_protocol.models.WorkNodeKind`):

| Node | PADOS record | Kind |
|---|---|---|
| Project | `projects` | PROJECT |
| Goal | a work request (title, text) with its task goal | GOAL |
| Task | a Task Plan of that goal | TASK |
| Subtask | a plan step | PLAN_STEP |
| Execution | a TaskLoop (under its plan) and each iteration with its job (under the step it ran) | (TaskLoop / job) |
| Evidence | a recorded job artifact (type, SHA-256) | ARTIFACT / RECEIPT |
| Decision | the WORK_ROUTE plan approval; the ResultCandidate decision | DECISION |

Status, blocking reason and next step are derived from PADOS statuses only (for example a pending WORK_ROUTE approval,
a WAITING_HUMAN loop, a ResultCandidate waiting for the WebAuthn step-up). A result is shown as accepted only when an
APPROVE decision is recorded. Expanded nodes, the selection and the view mode (mind map or list) live in page memory for
the session; no browser storage.

The generic graph workspaces (`/api/graph/workspaces/*`, `pados.graph`) and the Remote Work Map
(`/remote/v1/work-map`) were inspected and are not merged into this view: the first is a separate, local-only
knowledge-graph store whose nodes are edited freely, and editing such a node must never count as accepting governed
work; the second is the Remote console's per-actor task/run summary. Governed actions from the map reuse the existing
flow: "New work request from here" opens the work-request page (`POST /control/work-requests`, then the WORK_ROUTE
approval, task goal, plan and TaskLoop routes); "Open this run" resumes an existing TaskLoop; a waiting ResultCandidate
links to the Remote console step-up.

## Resources (`GET /control/resources`)

Every metric carries `source`, `unit`, `measured_at`, `measured` and `status` (`MEASURED` or `UNKNOWN`). A metric that
was not measured has no value and a closed `reason`; estimates carry `estimate=true`.

| Metric | Source | UNKNOWN when |
|---|---|---|
| `ram_in_use` | `psutil.virtual_memory` (host) | the probe fails |
| `pados_process_rss` | `psutil.Process.memory_info` (this server) | the probe fails |
| `model_resident_memory` | Ollama `/api/ps` via the loopback fetch of `pados.local_intelligence.runtimes` | no provider is configured as `ollama` (`MODEL_RUNTIME_NOT_CONFIGURED`; no call is made), the endpoint is not loopback, the runtime is unreachable or reports no sizes |
| `database_size` | `pg_database_size` of the PADOS databases on this server | the database is unavailable |
| `database_index_size` | `pg_indexes_size` (current database) | the database is unavailable |
| `project_documents`, `cache_size`, `evidence_files`, `operational_logs` | `pados.lifecycle.accounting` (sizes only, bounded no-follow walk) | the location is not configured or does not exist yet |
| `execution_records` | row counts and per-day counts from the rows' own timestamps (`worker_jobs`, `task_loop_iterations`, `worker_job_events`, `worker_job_artifacts`, `control_sync_events`) | the database is unavailable |
| `token_usage` | `worker_jobs.usage_metadata.measured_execution.runtime` (`prompt_eval_count`, `eval_count`), 30 days | no measured model execution exists |
| `model_execution_time` | same evidence, `total_duration` | no measured model execution exists |
| `job_execution_time` | `worker_jobs.started_at` / `completed_at`, 30 days | no finished job in the window |
| `reclaimable_estimate` | the cleanup plan preview (estimate) | never (0 when nothing is due) |

**Trends.** Execution-record growth is computed from existing timestamps (14 days). Storage size trends would need size
samples over time, which PADOS does not record (no new table by design): `trends.storage_bytes` is `UNKNOWN` with
`HISTORY_NOT_RECORDED`, and the page says so.

**Budget.** Configured by the existing storage policy document (`pados.lifecycle.storage_policy`), named by the optional
`PADOS_STORAGE_POLICY` setting; the level is the existing `pressure_of`. Defaults with no policy:

| Setting | Default | Meaning |
|---|---|---|
| `local_budget_bytes` | unset | no byte budget (shown as not configured) |
| `pressured_free_fraction` | 0.15 | warning when disk free ≤ 15 % (or budget used ≥ 85 %) |
| `critical_free_fraction` | 0.05 | critical when disk free ≤ 5 % (or budget used ≥ 95 %) |
| `min_free_bytes` | 0 | reserve kept free |

Unknown free disk space counts as PRESSURED (fail safe), as in Storage Flexibility V1. A policy file that does not
validate falls back to the defaults and reports `POLICY_INVALID:<code>`.

**Cleanup plan (preview only).** Candidates come from the accounting's retention-window estimate. A location is a
candidate only if it is a cleanup class (CACHE, SANDBOX_DATA, OPERATIONAL_LOGS) and every data kind in it is R0/R1 with
the `DELETE_RECORD` rule. Never candidates: evidence (job artifacts, receipts, release, project and capability-stage
evidence), decisions and audit records (R3 authority metadata, ledgers, tombstones), unique copies (content-addressed
objects, project sources, uploaded or staged records) and database rows. The plan has `deletes=false`,
`authority="NONE"` and `execute.available=false`: PADOS has no governed, reversible cleanup path (the purge library is
operator-invoked and irreversible), so the page shows "需要 Owner 授權" and no delete button.

## Verification

- Unit: `tests/control/test_resource_metrics.py` (each collector, its UNKNOWN path, the assembled summary and budget),
  `tests/control/test_cleanup_plan.py` (every data kind; protected classes are never selected),
  `tests/control/test_product_control_routes.py` (both routes, credential required).
- Web: `web/src/work-map.test.ts`, `web/src/resources.test.ts` (tree building, states, layout, memory, page states).
- Live: `scripts/dogfood/work_map_resources_e2e.py --pg-bin <PostgreSQL bin> --playwright` runs the product demo flow on a
  private PostgreSQL and `pados serve`, checks both projections over HTTP with a paired session, and runs
  `web/e2e/work-map-resources-live.spec.ts`.

## Not in V1

- Per-project storage budgets on the page (the policy supports `project_budget_bytes`; not shown yet).
- Storage size history and its trend (needs samples; deliberately no new table).
- Compute-node model memory (Compute Node status is shown in Mission Control; only the local configured runtime is
  read here).
- Any cleanup execution.
