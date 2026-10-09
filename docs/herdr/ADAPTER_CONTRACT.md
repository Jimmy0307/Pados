# Herdr adapter contract: `pados.herdr.telemetry.v1`

This is the PADOS-side contract. The live Herdr input format is **UNVERIFIED**
(HERDR_LIVE_UAT = PENDING; see [HERDR_INTEGRATION_V1.md](HERDR_INTEGRATION_V1.md) section 8).

v1 was finalized in the CR-06 fixer round, before any live deployment. Rows written by the earlier
draft (which had `agent.label`, raw ids and `observed_at`) do not validate as v1. They are never
read and are pruned on the next write.

## 1. Telemetry record (output; persisted)

The record is a pydantic model, `pados.herdr.contract.HerdrTelemetryRecord`, with
`frozen=True, extra="forbid"`. The persisted row is `record.to_row()` and has exactly the keys in
`RECORD_KEYS`. No field holds source-controlled free text.

| Field | Type / constraint | Source |
|---|---|---|
| `schema_id` | literal `pados.herdr.telemetry.v1` | PADOS |
| `machine_ref` | `^mach-[0-9a-f]{20}$` | PADOS configuration (`machine_id`); never a hostname, never taken from Herdr |
| `session_ref` | `^hs-[0-9a-f]{16}$` | `hs-` + the first 16 hex of HMAC-SHA256(store key, `hs` NUL raw session id) |
| `pane_ref` | `^hp-[0-9a-f]{16}$` | `hp-` + the first 16 hex of HMAC-SHA256(store key, `hp` NUL raw session id NUL raw pane id) |
| `agent_kind` | `HerdrAgentKind`: CLAUDE_CODE, CODEX, GEMINI, SHELL, OTHER, UNKNOWN | Mapped from known source values only; any other value is OTHER, a missing value UNKNOWN |
| `state` | `HerdrAgentState`: WORKING, IDLE, BLOCKED, WAITING_INPUT, EXITED, UNKNOWN | Mapped from the raw state; telemetry only |
| `collected_at` | timezone-aware ISO-8601 | PADOS collector clock at `collect()` |
| `source_observed_at` | timezone-aware ISO-8601 or null | Document-level source snapshot time, only if proven (section 4); null otherwise |
| `last_transition_at` | timezone-aware ISO-8601 or null | From Herdr; dropped if naive, unparseable or more than 60 s after `collected_at` |
| `health` | `HerdrHealth`: HEALTHY, DEGRADED, UNREACHABLE | Source and pane health, not PADOS health |
| `project_ref` | PADOS id or null | Operator binding only (`bindings[tag].project_ref`) |
| `work_package_ref` | PADOS id or null | Operator binding only (`bindings[tag].work_package_ref`) |
| `semantic_authority` | literal `NONE` | PADOS |

The record has no field for labels, terminal content, scrollback, pane buffers, prompts,
messages, context, credentials, approvals, receipts or PADOS run states
(`TERMINAL_HISTORY_DEFAULT = NOT_PERSISTED`).

The raw session id, pane id, label and tag are used only as HMAC or lookup input and are never
stored. Raw ids must be a string or integer of 1 to 256 characters.

Each row passes these guards before it leaves the adapter and again before every write:

```
assert_minimized(row, allowed_keys=RECORD_KEYS, max_string=128, max_items=13, max_total_bytes=4096)
assert_no_authority(row)
```

`assert_no_authority` checks these properties:

- the key set is exact;
- `schema_id` and `semantic_authority == NONE` are present;
- `state`, `health` and `agent_kind` come from the closed enums;
- every value is a scalar;
- no value is a PADOS authority term.

## 2. Store document (persisted)

The store document has this shape:

```json
{"schema_id": "pados.herdr.telemetry.v1", "kind": "HERDR_TELEMETRY",
 "retention_class": "R1_OPERATIONAL", "semantic_authority": "NONE",
 "records": [<row>, ...],
 "collections": [{"at": "<aware ISO-8601>", "outcome": "<HerdrCollectionOutcome>",
                  "code": "<HerdrDiagnostic> | null"}, ...],
 "last_document_digest": "<32 hex keyed digest> | null"}
```

- The file has mode 0600 and is written atomically.
- `collections` keeps at most 16 entries (`MAX_COLLECTIONS`) and is pruned by R1 age.
- `HerdrCollectionOutcome` takes these values:

  | Outcome | When |
  |---|---|
  | `COLLECTED` | The document was collected in full |
  | `COLLECTED_PARTIAL` | Some entries were refused |
  | `UNCHANGED` | Same keyed document digest as the last collected document: nothing appended, freshness not refreshed |
  | `DOCUMENT_REFUSED` | The whole document was refused |
  | `SOURCE_UNREACHABLE` | The source failed |

- The digest key is a separate file, `<store>.refkey`: 32 random bytes, mode 0600, created on
  first use under the store lock. A group- or world-readable, truncated or symlinked key file is
  refused. The key is never logged, projected or written into the store document.
- Admission bounds:
  - The file is size-checked (`max_bytes`) before it is parsed. It is opened no-follow and
    non-blocking; a non-regular or oversized file reads as empty.
  - Stored arrays are truncated to the newest `max_records` rows and 16 collections before they
    are validated.
  - An incoming batch is bounded with `islice` (at most 256 records) before it is accumulated.

## 3. Latest projection (read-only; not persisted)

`HerdrTelemetryStore.latest(stale_after_seconds=..., not_installed=False)` returns one entry per
(`machine_ref`, `session_ref`, `pane_ref`). It carries:

- the record's references, `agent_kind`, `collected_at`, `source_observed_at`,
  `last_transition_at`, `project_ref`, `work_package_ref` and `semantic_authority: NONE`;
- `state`: the observed state when live, otherwise `UNKNOWN`;
- `health`: the observed health when live, otherwise null;
- `last_observed_state` and `last_observed_health`;
- `telemetry_meaning` (of the shown state) and `meaning_scope: TELEMETRY_ONLY`;
- `source_age_seconds`, `stale` and `stale_reason`.

A row is **live** only when all of these hold:

- the source is installed;
- the latest collection outcome is COLLECTED, COLLECTED_PARTIAL or UNCHANGED;
- the row's health is not UNREACHABLE;
- `source_observed_at` is proven and no older than `stale_after_seconds`.

Otherwise it is stale, and `stale_reason` is one of `SOURCE_NOT_INSTALLED`, `NOT_COLLECTED`,
`LATEST_COLLECTION_UNHEALTHY`, `SOURCE_HEALTH_UNREACHABLE`, `SOURCE_TIME_UNPROVEN` or
`SOURCE_TIME_EXPIRED`.

## 4. Raw input (UNVERIFIED expectation)

The adapter takes a raw document of at most 256 KiB, at most 16 levels deep, with at most 64
sessions and 256 panes:

```json
{"snapshot_at": "<aware ISO-8601 snapshot time>",
 "sessions": [
  {"session_id": "<id>",
   "panes": [
     {"pane_id": "<id>",
      "agent": {"kind": "claude"},
      "state": "working",
      "last_transition_at": "<aware ISO-8601>",
      "health": "healthy",
      "tag": "<source tag looked up in the operator bindings>"}]}]}
```

| Element | Accepted keys or values |
|---|---|
| Source time | `snapshot_at`, then `generated_at`, then `observed_at` (document level) |
| Session id | `session_id`, then `id` |
| Pane id | `pane_id`, then `id` |
| State | `state`, then `agent_state`, then `status` |
| Transition time | `last_transition_at`, then `state_changed_at` |
| Tag | `tag`, then `project_tag` |
| `agent` | an object (only `kind` is read) or a plain string (the kind) |
| Agent kind (normalized) | `claude`, `claudecode`, `codex`, `codexcli`, `gemini`, `geminicli`, `shell`, `bash`, `zsh`, `sh`, `fish`, `pwsh`, `powershell` |
| Raw state, normalized (lower-case, separators removed) | `working`, `idle`, `blocked`, `waitinginput` / `waiting`, `exited`; anything else is UNKNOWN |
| Raw health | `healthy`, `degraded`, `unreachable`; absent is HEALTHY, anything else is DEGRADED |

The source time is proven only if it is timezone-aware, no more than 60 s in the future and no
older than `stale_after_seconds` at collection. If it is not proven, the records are still stored
with `source_observed_at = null` and a `source_time_code`, and they are never projected live:

| Source time code | Meaning |
|---|---|
| `HERDR_SOURCE_TIME_MISSING` | No source-time key in the document |
| `HERDR_SOURCE_TIME_INVALID` | Unparseable or naive timestamp |
| `HERDR_SOURCE_TIME_IN_FUTURE` | More than 60 s ahead of the collector clock |
| `HERDR_SOURCE_TIME_TOO_OLD` | Older than `stale_after_seconds` at collection |

Ignored and counted (never copied):

- **Terminal content**: any key containing `scrollback`, `buffer`, `output`, `lines`, `history`,
  `screen` or `content`, at any depth.
- **Authority claims** (`AUTHORITY_CLAIM_KEYS`, including raw `project_ref` and
  `work_package_ref`).
- **Identity claims** (`IDENTITY_CLAIM_KEYS`).
- **Agent labels** (`label`, `name`, `title`) and unbound tags, counted as `fields_dropped`.

## 5. Collect result and diagnostics (closed vocabularies)

`CollectResult.summary()` has these keys:

- `schema_id`, `collected_at`, `outcome`, `source_health`;
- `records` (a count);
- `error_code`, `source_observed_at`, `source_time_code`;
- `terminal_content_dropped`, `authority_claims_ignored`, `identity_claims_ignored`;
- `fields_dropped`, `entries_refused`;
- `refusal_codes` (at most 8 codes);
- `semantic_authority: NONE`.

Every code is a `pados.herdr.diagnostics.HerdrDiagnostic` member. A source error that is not one
of them, including any exception a source raises, becomes `HERDR_SOURCE_ERROR`. Exception text is
never echoed.

| Category | Codes |
|---|---|
| Document refusals | `HERDR_RAW_DOC_NOT_BYTES`, `HERDR_RAW_DOC_TOO_LARGE`, `HERDR_RAW_DOC_INVALID_JSON`, `HERDR_RAW_DOC_SHAPE_INVALID`, `HERDR_RAW_DOC_TOO_DEEP`, `HERDR_RAW_DOC_TOO_MANY_SESSIONS`, `HERDR_RAW_DOC_TOO_MANY_PANES` |
| Source time | `HERDR_SOURCE_TIME_MISSING`, `HERDR_SOURCE_TIME_INVALID`, `HERDR_SOURCE_TIME_IN_FUTURE`, `HERDR_SOURCE_TIME_TOO_OLD` |
| Source failures | `HERDR_NOT_INSTALLED`, `HERDR_SOURCE_TIMEOUT`, `HERDR_SOURCE_EXIT_NONZERO`, `HERDR_SOURCE_UNAVAILABLE`, `HERDR_FIXTURE_UNAVAILABLE`, `HERDR_FIXTURE_UNREADABLE`, `HERDR_COMMAND_SOURCE_UNSUPPORTED_ON_WINDOWS`, `HERDR_SOURCE_ERROR` |
| Entry refusals | `HERDR_REF_INVALID`, `HERDR_RECORD_INVALID`, `HERDR_RECORD_NOT_MINIMIZED`, `HERDR_RECORD_CLAIMS_AUTHORITY`, `HERDR_PANE_DUPLICATE`, `HERDR_PANE_SHAPE_INVALID`, `HERDR_SESSION_SHAPE_INVALID` |

## 6. Command source

`CommandHerdrSource` runs `herdr status --json` (UNVERIFIED), an allowlisted argv with no shell.

- **POSIX.**
  - One monotonic deadline covers reading and waiting.
  - The output cap is 256 KiB; the read stops at the cap plus one byte.
  - The child runs in its own session (`start_new_session=True`). Its whole process group is
    killed (`os.killpg`, SIGKILL) on timeout, on crossing the cap and after the leader exits; the
    unreaped leader keeps the group id reserved until then.
  - The child gets a minimal environment.
- **Windows.** The source is disabled. `installed()` is false and `fetch()` and `bounded_run` raise
  `HERDR_COMMAND_SOURCE_UNSUPPORTED_ON_WINDOWS`. It stays disabled until bounded streaming with a
  job object exists.

## 7. Versioning

A change to `RECORD_KEYS`, to the enums or to the reference patterns is a new schema id
(`pados.herdr.telemetry.v2`). Rows with an unknown `schema_id` are never read as v1.
