# PADOS vNext Current-State Inventory

Status: `ARCHITECTURE BASELINE`; implementation authority: not granted.

## Evidence scope

This inventory is a read-only snapshot of branch `codex/p6-11-project-goal-loop` at `1f08308757585d7229bcd2c3ba2ca27b1307ffa0`. Existing unstaged P6.11 work is excluded from architectural acceptance and is not changed by this document pack.

| Area | Current module / file | Current responsibility and dependency | State | vNext disposition | Risk |
| --- | --- | --- | --- | --- | --- |
| Project registry | `core/task_service.py`, migration `0001` | Creates `projects`; retrieval separately maintains `context_project_registry` | Present, duplicated registry concept | Reuse project identity; converge ownership later | High |
| Task state machine | `core/state_machine.py`, `core/task_service.py` | Validates core task transitions and writes audit records | Present | Reuse as a governed aggregate; consolidate loop/task terminology | Medium |
| Workflow / agent loop | `loop/*`, `project_goal/*`, `agents/loop.py` | P6.10 loop routes work; P6.11 composes P6.10; legacy loop also calls retrieval/provider/task services | Present, mixed layers | Preserve P6.10/P6.11 semantics; remove cross-layer execution coupling in a future phase | High |
| Execution / provider abstraction | `providers/*`, `worker/*`, `core/router.py` | Protocol/factory plus Codex, Claude, Ollama and mock adapters; worker router executes registered commands | Partial | Retain adapter direction; replace provider selection default and model assumptions with governed runtime contracts | High |
| Context / retrieval | `memory/*`, `retrieval/indexer.py`, `models/context_memory.py` | Registry, retrieval, token budget and context packets; uses Memory terminology and direct SQL access | Present, misnamed for vNext | Reframe as Context Service Foundation; defer Knowledge Memory | High |
| Artifact management | `memory.repository`, `worker/handoff.py`, migrations `0001`, `0003`, `0005` | Hash pointers, handoff artifacts and worker job artifacts | Partial | Reuse hashes/lineage; unify immutable artifact contract | Medium |
| Validation | `test_runs`, `review_findings`, loop and worker validations | Validation state appears in several aggregates | Partial | Establish a first-class validation/evidence aggregate | High |
| Approval | `control/service.py`, `project_goal/service.py` | Work-request approvals and P6.11 human gates | Present, split | Reuse human-gate principles; unify policy-scoped approval commands | High |
| Release | `release/package.py`, maker-checker | Package construction and release eligibility | Partial | Keep release safety checks; introduce release aggregate and immutable manifest | Medium |
| Audit / evidence | `audit_events`, append-only run/worker/loop/project events | Append-only triggers exist for several event streams | Strong foundation, fragmented | Preserve append-only rule; normalize correlation and evidence links | Medium |
| Database models | SQL text repositories and migrations `0001`–`0009` | PostgreSQL is operational state, with pgvector and immutable triggers | Strong foundation | No schema action now; define logical ownership before later migrations | Medium |
| API / CLI | `api/app.py`, `cli/main.py` | FastAPI/Typer surfaces import control, loop, memory and worker services directly | Present | Keep external contracts where safe; route through vNext application commands | High |
| Security / repository boundary | `control/security.py`, `retrieval/indexer.py`, `worker/command.py`, `memory/security.py` | Localhost auth, root validation, secret checks, command allowlists | Strong foundation | Retain fail-closed rules and centralize policy decisions | High |
| Worktree / sandbox | worker schemas, command service, runtime contract | Isolation types and bounded project-root command execution | Partial | Define execution-mode contract before expanding automation | High |
| Existing tests | `tests/unit`, `tests/integration`, `tests/security`, `tests/contract` | Coverage for P6.6–P6.11, security and DB contracts | Strong foundation | Freeze as no-regression baseline; add characterization tests before refactor | Medium |
| Architecture documents | root docs and `docs/PADOS_*` | P0/P6.x runtime and phase documents | Present but internally stale | Keep as legacy evidence; vNext documents are proposed governance overlay | Medium |

## Existing persistence groups

- Core: `projects`, `tasks`, `task_dependencies`, `agent_runs`, `agent_handoffs`, file/wiki/decision/test/review/artifact/checkpoint records.
- Context foundation: context projects, sources, memory records/relationships/decisions/artifacts, runs/events, packets/items.
- Control and execution: control messages/work requests/approvals/sync events; worker jobs/events/artifacts.
- Goal loops: task goals/plans/steps/loops/iterations/snapshots/deltas/leases/events and project goals/plans/tasks/dependencies/cycles/gaps/replans/human gates/events.

## Current-state conclusions

1. PostgreSQL, append-only events, project-root validation, command allowlists and human gates are reusable vNext foundations.
2. `memory` currently contains both Context Service responsibilities and Phase-2 knowledge-like responsibilities. It must not be expanded under its current ownership model.
3. The direct dependencies from API/CLI and loop services to routing, memory and provider code prevent a clean control-plane boundary.
4. Existing P6.11 work is a legacy compatibility input, not an approved vNext implementation baseline.
