# Herdr Integration V1 (contract level)

Status: contract-level implementation. **HERDR_LIVE_UAT = PENDING.** Herdr is not installed on
the build machine. Nothing here has been run against a live Herdr, and the live wire format is
**UNVERIFIED**.

Code: `src/pados/herdr/` · Tests: `tests/herdr/` · Fixtures: `tests/fixtures/herdr/` ·
Schema: [ADAPTER_CONTRACT.md](ADAPTER_CONTRACT.md)

## 1. Roles

| Component | Role | Never |
|---|---|---|
| Herdr | Agent runtime telemetry and session substrate: it reports which agent panes exist and what state they appear to be in | Scheduler, router, semantic, Memory, provider, approval or audit authority |
| PADOS | Governed authority and orchestration layer | Treats Herdr output as truth, acceptance or context |

Frozen principles that apply here:

- HERDR WORKING != PADOS ACCEPTED
- HERDR IDLE != PADOS SUCCESS
- HERDR BLOCKED != PADOS FAILED
- HERDR SESSION != PADOS MEMORY
- HERDR TERMINAL HISTORY != PROJECT CONTEXT
- MODEL OUTPUT != TRUTH. Installed or registered != authorized.

Herdr output is R1 operational telemetry (`DataKind.HERDR_TELEMETRY`). It never feeds
`add_memory_record`, never feeds semantic state, never becomes an ExecutionReceipt or
ResultReceipt, and never reaches CAS or the audit chain (THREAT_MODEL_V1 section 7, control
SH-H01).

## 2. State semantics

Every Herdr state maps to a **telemetry-only** observation (`pados.herdr.boundary.STATE_MEANING`,
`MEANING_SCOPE = TELEMETRY_ONLY`). None of these is a PADOS run, job, validation or acceptance
state. No function derives acceptance, success or failure from a Herdr state, and a static test
enforces this.

| HerdrAgentState | Telemetry meaning | It does NOT mean |
|---|---|---|
| WORKING | AGENT_ACTIVITY_OBSERVED | the work package is progressing correctly or will be accepted |
| IDLE | AGENT_IDLE_OBSERVED | the task succeeded or is complete |
| BLOCKED | AGENT_BLOCKED_OBSERVED | the run failed |
| WAITING_INPUT | AGENT_INPUT_REQUEST_OBSERVED | an approval is pending in PADOS (PADOS approvals live only in PADOS) |
| EXITED | AGENT_EXIT_OBSERVED | the job finished, succeeded or produced a valid receipt |
| UNKNOWN | AGENT_STATE_NOT_OBSERVED | anything at all; it covers unknown raw states and every stale row |

Health (`HEALTHY`, `DEGRADED`, `UNREACHABLE`) describes the telemetry source. It never describes
PADOS.

A stale row is shown with `state: UNKNOWN`, `health: null`, `stale: true` and a closed
`stale_reason`. The last observed values appear only as `last_observed_state` and
`last_observed_health`. These rules extend SH-N05, SH-N06 and SH-N11 with the CR-06 findings
(H02, M01). A row is **live** only when all of these hold:

- the source is installed;
- the latest persisted collection outcome is COLLECTED, COLLECTED_PARTIAL or UNCHANGED;
- the row's health is not UNREACHABLE;
- the row's `source_observed_at` is proven and no older than `stale_after_seconds`.

`source_observed_at` is the **document-level** snapshot time reported by the source (UNVERIFIED
key `snapshot_at`). It is proven only if it is timezone-aware, no more than 60 s in the future
and no older than `stale_after_seconds` at collection. PADOS's own `collected_at` never stands in
for it.

A repeated document (same keyed digest) is recorded as `UNCHANGED` and appends nothing. A cached
or replayed document therefore cannot refresh freshness.

## 3. Adapter contract (summary)

`pados.herdr.adapter.HerdrAdapter(source, machine_ref=..., ref_key=..., bindings=...,
stale_after_seconds=...)`:

- **Polls only on request.** The adapter polls only when a caller invokes `collect()`, or
  `HerdrConfig.collect()`, which also records the result in the store. It starts no timer, thread
  or scheduler. It has no callback into PADOS, so Herdr cannot start, cancel, route, approve or
  commit anything.
- **Sources.** The `HerdrSource` protocol is `fetch() -> bytes` and `installed() -> bool`.
  Presence is not runtime availability: runtime availability is the latest persisted collection
  outcome.
  - `FixtureHerdrSource(path)`: tests and offline UAT. It reads at most the cap plus one byte.
  - `CommandHerdrSource()`: defined, never invoked by tests, and its argv is UNVERIFIED.
    - It runs the fixed allowlisted argv `herdr status --json`, resolved on `PATH`, and never
      uses `shell=True`.
    - **POSIX:** one monotonic deadline covers reading and waiting (default 5 s, maximum 30 s).
      The output cap is 256 KiB. The child runs in its own session and process group, and the
      whole group is killed on timeout, on crossing the cap and after the leader exits, so
      descendants never outlive a collection. The child gets a minimal environment.
    - **Windows:** disabled (`HERDR_COMMAND_SOURCE_UNSUPPORTED_ON_WINDOWS`, fail closed) until
      bounded streaming with a job object exists. `installed()` is false there.
- **Bounds.** A raw document is limited to 256 KiB, a nesting depth of 16, 64 sessions and 256
  panes (the pane count is checked while accumulating). Anything larger, invalid JSON or the
  wrong top-level shape is refused whole.
- **No source-controlled free text** (CR-06 H01/M05).
  - Session and pane ids become keyed digests: `hs-` or `hp-` plus 16 hex of HMAC-SHA256 under the
    per-store key.
  - The agent becomes a closed `HerdrAgentKind`, mapped from known source values only.
  - Labels are never read.
  - Project and work-package references come only from the operator's `bindings` map, looked up by
    the pane's source tag. The tag itself is never kept, and raw `project_ref` and
    `work_package_ref` fields are ignored as claims.
- **Terminal content.** Any raw key that contains `scrollback`, `buffer`, `output`, `lines`,
  `history`, `screen` or `content` is dropped at any depth and counted
  (`terminal_content_dropped`). It is never inspected further and never copied.
- **Authority and identity claims.** These are ignored and counted (`authority_claims_ignored`,
  `identity_claims_ignored`). `machine_ref` comes from PADOS configuration only.
- **Diagnostics** are the closed `HerdrDiagnostic` enum. Any unfamiliar source error, including
  any exception, becomes `HERDR_SOURCE_ERROR`, and exception text is never echoed.
- **Guards.** Every record passes `assert_minimized` (explicit `RECORD_KEYS`) and
  `assert_no_authority`, in the adapter and again in the store.

`CollectResult.outcome` and `source_health` take these values:

| Outcome | Source health | When |
|---|---|---|
| `SOURCE_UNREACHABLE` | `UNREACHABLE` | The source failed |
| `DOCUMENT_REFUSED` | `DEGRADED` | The whole document was refused |
| `COLLECTED_PARTIAL` | `DEGRADED` | Some entries were refused |
| `COLLECTED` | `DEGRADED` | The source time was not proven |
| `COLLECTED` | `HEALTHY` | Otherwise |

## 4. Privacy

- `TERMINAL_HISTORY_DEFAULT = NOT_PERSISTED`. The record schema has no field for labels, terminal
  content, scrollback, pane buffers, prompts or output. Tests plant label, terminal, encoded-id
  and credential-shaped markers and check that none of them reaches the store, the projection or
  the diagnostics.
- References are:
  - a PADOS `machine_id` (`mach-<20 lowercase hex>`, never a hostname);
  - PADOS keyed digests for Herdr session and pane ids;
  - operator-bound PADOS project and work-package ids.
- The digest key `<store>.refkey` holds 32 random bytes with mode 0600 and is created on first use
  under the store lock. A key file that is group- or world-readable, truncated or symlinked is
  refused. The key is never logged, projected or written into the store document. The keyed
  document digest identifies a repeated document without revealing its content.
- Diagnostics hold counts and closed codes only, never raw values.

## 5. Retention and storage

`pados.herdr.store.HerdrTelemetryStore` is `DataKind.HERDR_TELEMETRY`, `R1_OPERATIONAL`,
`PurgeRule.DELETE_RECORD`. Its writer is `HerdrTelemetryStore.record_collection`, and the
inventory entry `herdr_telemetry` records `may_hold_prohibited=False`.

- It is a single JSON file holding the rows, a bounded content-free collection log (at most 16
  entries of `{at, outcome, code}`) and the last document digest. It is written atomically (temp
  file, fsync, replace) with mode 0600 under the cross-process store lock. A symlinked store file
  is refused.
- **Admission (CR-06 M02).**
  - The file is opened no-follow and non-blocking and is size-checked against `max_bytes` before
    it is parsed. An oversized, non-regular (for example a FIFO) or corrupt file reads as empty
    and is replaced on the next write.
  - Stored arrays are truncated to the newest `max_records` rows and 16 collections before they
    are validated, so a read never returns more than `max_records`.
  - Incoming batches are bounded with `islice` (at most 256 records) before they are accumulated.
- **Pruning.** Every write prunes these rows:
  - rows older than the R1 window (`RetentionPolicy().r1`, default 14 days; a shorter window may
    be configured, a longer one may not);
  - future-dated or unparseable rows;
  - the oldest rows beyond `max_records` (default 2000, maximum 10000) or `max_bytes` (default
    1 MiB, minimum 8 KiB, maximum 16 MiB).
- Reads never write. A row on disk that is not exactly a valid telemetry record is never returned,
  and the next write or `prune()` removes it.
- Lifecycle storage accounting measures the store directory, including the key file, as
  `HerdrConfig.store_root`.

## 6. Configuration and visibility

`PADOS_HERDR_CONFIG` names an optional JSON file; the schema is in `pados.herdr.config` and
includes `bindings`. PADOS is fully functional without it.

| Condition | Status |
|---|---|
| Unset | `HERDR_NOT_CONFIGURED` |
| Unreadable, unparseable, unknown key or invalid value | `HERDR_CONFIG_INVALID` (with a reason), whole file |
| `enabled: false` | `HERDR_DISABLED` |
| Source not installed, or latest collection `SOURCE_UNREACHABLE` | `HERDR_UNAVAILABLE` (every row stale) |
| Latest collection `DOCUMENT_REFUSED` | `HERDR_DEGRADED` (every row stale) |
| No collection yet | `HERDR_NOT_OBSERVED` |
| Otherwise | `HERDR_OBSERVED` |

`pados.herdr.visibility.herdr_visibility(config_path) -> dict` is the read-only Mission Control and
doctor block:

- It never collects, executes, writes, creates the key or schedules.
- It reports `source_installed` (presence) separately from `source_runtime` (`NOT_COLLECTED`,
  `REACHABLE`, `UNREACHABLE` or `DOCUMENT_REFUSED`, taken from the latest collection outcome), and
  shows `latest_collection`.
- It always carries `semantic_authority: NONE`, `meaning_scope: TELEMETRY_ONLY`,
  `terminal_content: NOT_PERSISTED` and `herdr_live_uat: PENDING`.

## 7. Integrator wiring

Already wired in `9df786d`:

- the `PADOS_HERDR_CONFIG` setting;
- read-only `herdr` blocks in doctor and Mission Control;
- the storage-report `herdr_root`;
- the inventory entry.

If periodic collection is wanted, call `HerdrConfig.collect()` from the existing connectivity
background tick or from doctor. Do not add a new scheduler.

## 8. Live UAT pending list (HERDR_LIVE_UAT = PENDING)

Run these once Herdr is installed on a PADOS machine. Each item needs command, exit code and
evidence before the mapping is marked verified.

1. **Real status JSON shape.** Capture the actual output of the status command and confirm the
   subcommand and flags (`herdr status --json` is a placeholder). Confirm the top-level `sessions`
   list, a **document-level snapshot timestamp** (`snapshot_at`, `generated_at` or `observed_at`),
   the per-session id key, the `panes` list, and the per-pane id, agent kind, state, transition
   time, health and tag keys. Update `HERDR_STATUS_ARGS`, the key aliases in `adapter.py` and the
   fixtures to match the captured shape, with secrets and terminal content removed. If Herdr has
   no snapshot timestamp, every row stays UNKNOWN (fail closed) until one is provided.
2. **State names and agent kinds.** List every raw state and agent kind Herdr emits. Confirm the
   mappings in `RAW_STATE_MAP` and `AGENT_KIND_MAP`. Every other state must stay UNKNOWN and every
   other kind OTHER. Confirm that no raw value carries PADOS meaning.
3. **Pane and session ids.** Confirm the ids are stable across polls; the keyed digests are only
   stable if the ids are. Confirm the per-session uniqueness assumption (the pane digest includes
   the session id). Confirm the ids stay within 256 characters.
4. **Scrollback absence in the store.** Run real agents that print distinctive marker text and
   collect several times. Grep the store file, the projection and every PADOS log for the marker;
   the expected result is zero hits. Record `terminal_content_dropped` for the real document.
5. **Document size.** Record the real document size with many panes. Confirm it stays under the
   256 KiB cap in normal use, or adjust the cap with evidence.
6. **Latency.** Measure the wall time of one `collect()` (p50 and p95 over at least 20 runs) and
   confirm it is well under the 5 s timeout.
7. **Failure modes.** For each case, record the collection outcome, the code and the visibility
   status (expected: HERDR_UNAVAILABLE or HERDR_DEGRADED, never a crash and never live data):
   - Herdr not installed (`HERDR_NOT_INSTALLED`);
   - Herdr daemon stopped;
   - a hung command (timeout, with no surviving descendants);
   - non-zero exit;
   - truncated or invalid JSON;
   - oversized output;
   - permission denied.
8. **Clock and staleness.** With the daemon stopped, or returning a cached document, confirm that
   rows become `stale: true` with state UNKNOWN after `stale_after_seconds`.
9. **Environment.** Confirm Herdr works with the minimal child environment (`PATH`, home and temp
   variables). If it needs more, add only the named, non-secret variables.
10. **Windows.** The command source is disabled on Windows
    (`HERDR_COMMAND_SOURCE_UNSUPPORTED_ON_WINDOWS`). Enabling it requires a bounded streaming
    reader plus a job object that kills descendants, tested like the POSIX CR-06 M04 tests.

## 9. Residual risks

- **Unverified live format.** Live Herdr may use other keys, in which case collections yield
  refusals, UNKNOWN states or unproven source times. This fails closed (no false liveness) and is
  UAT items 1 to 3.
- **Plausible but false telemetry.** A compromised Herdr can report false agent states and a fresh
  snapshot timestamp. Impact is limited to telemetry display: no PADOS authority path exists,
  states are labelled telemetry-only, and no source-controlled free text is shown.
- **Key protection.** The digest key protects ids only against offline guessing by someone who has
  the store file but not the key file. Both sit in the same 0600 directory.
- **Process-group reuse.** On POSIX the process group is killed while the unreaped leader still
  reserves its id. Descendants that call `setsid()` themselves escape the group.
- **Over-counting.** Terminal-content key detection is substring-based, so it may over-count
  unrelated keys (for example `deadlines`). Those keys are never copied in any case.
