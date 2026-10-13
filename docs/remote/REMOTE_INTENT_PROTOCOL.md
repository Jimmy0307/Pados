# Remote Intent Protocol

**REMOTE CLIENT SENDS INTENT, NOT CONTEXT.**

## Envelope (`pados.remote.intent.RemoteIntentEnvelope`)

| Field | Source | Notes |
| --- | --- | --- |
| `intent_id` | client | idempotency key; replays return the same work request |
| `project_id` | client | must be in the actor's allowlist |
| `actor_id`, `session_id` | **server** (authenticated request) | forbidden in payload |
| `intent_type` | client | `START_TASK`, `UPLOAD_AND_INGEST`, `QUERY_STATUS`, `APPROVE`, `REJECT`, `PAUSE`, `RESUME`, `CANCEL` |
| `human_message` | client | at most 2,000 characters; required for `START_TASK` |
| `attachment_refs` | client | `staged:<id>` references only (at most 16); the files must be STAGED and bound to the same project |
| `requested_capability` | client | optional identifier |
| `target_run_id` | client | required for approve/reject/pause/resume/cancel |
| `approval_ref` | client | required for approve/reject |
| `priority` | client | `LOW`, `NORMAL`, `HIGH` |
| `created_at` | server | UTC ISO-8601 |

## Invariants

* The envelope MUST NOT contain reconstructed project context. Payload keys
  `context`, `project_context`, `sources`, `semantic`, `history`, `chat_history`,
  `messages`, `conversation`, `memory`, `promoted_memory`, `scope_version`,
  `system_prompt`, and `provider_history` are rejected with `CLIENT_CONTEXT_FORBIDDEN`.
* Unknown keys are rejected with `UNKNOWN_FIELD`.
* **CHAT HISTORY IS NOT PADOS MEMORY.** No remote UI history is stored as memory
  or concatenated into context.

## Server-side context reconstruction

`pados.remote.context.RemoteContextReconstructor` rebuilds context with the
existing `pados.execution.context.ProjectContextLoader`. It combines the
authoritative semantic fields from the operational and proof lanes with the
bounded repository instruction files of the registered workspace. The result
records `context_source=PADOS_AUTHORITATIVE_STORE`, `client_history_included=false`,
and `provider_history_included=false`. It is never echoed back to the client;
only fingerprints and counts are returned.

## Ledger

Every intent and its outcome is appended to a hash-chained JSONL ledger
(`pados.remote.ledger.IntentLedger`). The ledger is operational evidence and not
semantic truth.
