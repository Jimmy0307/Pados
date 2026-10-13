# PADOS Data Lifecycle V1

Lifecycle is operational policy. It is not authority. Nothing in `pados.lifecycle` commits semantic
state, writes to the proof database, moves a ScopeVersion or bypasses CAS. Nothing in it runs on a
schedule either: there is no timer, daemon or background purge, and a purge happens only when an
operator calls `apply_purge` with the exact hash of a plan they reviewed.

| Module | Responsibility |
| --- | --- |
| `pados.lifecycle.retention` | Retention classes, storage categories, the `DataKind` catalog, `RetentionPolicy` (shared contract) |
| `pados.lifecycle.minimization` | `assert_minimized`: the guard every new persistence point calls before writing (shared contract) |
| `pados.lifecycle.accounting` | Storage location registry, per-category accounting, optional DB accounting |
| `pados.lifecycle.purge` | `plan_purge` (dry run, plan hash) and `apply_purge` (hash-approved, re-validated) |
| `pados.lifecycle.tombstones` | Append-only, hash-chained, two-phase tombstones for purged rejected payloads |
| `pados.lifecycle.rejection` | `RejectionAuthority` port and the read-only, conservative proof-DB implementation |
| `pados.lifecycle.propagation` | Delete/revoke propagation plan built on `LineageGraph.plan_revocation` |
| `pados.lifecycle.inventory` | Inventory of existing persistence points and their minimization findings |
| `pados.lifecycle.tables` | Map from DB table to category and data kind (accounting only) |
| `pados.lifecycle.fswalk` | Descriptor-pinned, no-follow, budgeted filesystem access (shared by accounting and purge) |

## Retention classes

| Class | Window | Meaning |
| --- | --- | --- |
| `R0_EPHEMERAL` | default 24h (1 min to 7 days) | temp files, sandbox scratch, raw intermediates, temporary uploads, rebuildable caches |
| `R1_OPERATIONAL` | default 14 days; outside 7-30 days needs `explicit_override=True` | logs, telemetry, connectivity history, machine probes, quarantined uploads |
| `R2_PROJECT` | none (project lifecycle) | accepted sources, active artifacts, project and release evidence: `HUMAN_DECISION_ONLY` |
| `R3_AUTHORITY` | none (durable) | authority metadata: ScopeVersion/CAS transitions, decisions, audit chain, hashes, receipts |

R3 means metadata, not "keep every byte forever". The one R3 kind with a purgeable payload is
`REJECTED_ARTIFACT_PAYLOAD`. Its `payload_retention` is `R1_OPERATIONAL`, counted from the timestamp of
the rejection decision. Its metadata is never purged.

The full catalog, with every `DataKind` and its category, class, purge rule and payload retention, is
in [RETENTION_MATRIX.md](RETENTION_MATRIX.md).

## Storage accounting

`build_locations(settings, runtime_root=..., herdr_root=None, sandbox_roots=(), tombstone_ledger=None)`
builds the location registry from configuration:

| Location id | Source | Category | Data kinds |
| --- | --- | --- | --- |
| `product_objects` | `PADOS_PRODUCT_OBJECT_ROOT` (content-addressed `ObjectStore`) | `ARTIFACTS` | `ACTIVE_ARTIFACT`, `REJECTED_ARTIFACT_PAYLOAD` |
| `product_workspace` | `PADOS_PRODUCT_WORKSPACE_ROOT` | `PROJECT_SOURCES` | `GOVERNED_PROJECT_CONTENT` |
| `remote_staging` | `PADOS_REMOTE_CONFIG` -> `RemoteConfig.staging_root` | `REMOTE_UPLOADS` | `TEMPORARY_UPLOAD`, `QUARANTINED_UPLOAD` |
| `remote_intent_ledger` | `PADOS_REMOTE_CONFIG` -> `RemoteConfig.ledger_path` | `AUTHORITY_METADATA` | `REMOTE_INTENT_LEDGER` |
| `compute_node_state` | `PADOS_COMPUTE_NODES_CONFIG` -> `NodeRegistry.state_dir` | `OPERATIONAL_LOGS` | `MACHINE_CONNECTION_HISTORY` |
| `connectivity_snapshot` | `PADOS_CONNECTIVITY_CONFIG` -> `snapshot_path` | `OPERATIONAL_LOGS` | `CONNECTIVITY_HISTORY` |
| `runtime_logs` | `<runtime>/logs` | `OPERATIONAL_LOGS` | `OPERATIONAL_LOG` |
| `runtime_cache` | `<runtime>/cache` | `CACHE` | `CACHE_ENTRY` |
| `release_test_evidence` | `<runtime>/artifacts/reports` | `RELEASE_TEST_EVIDENCE` | `RELEASE_TEST_EVIDENCE` |
| `herdr_telemetry` | injected root (no store exists yet) | `HERDR_TELEMETRY` | `HERDR_TELEMETRY` |
| `sandbox_<n>` | injected roots (e.g. `SimulatedSandboxAdapter(base_dir=...)`) | `SANDBOX_DATA` | `SANDBOX_WORKING_DATA`, `RAW_INTERMEDIATE_OUTPUT` |
| `payload_tombstones` | injected tombstone ledger path | `AUTHORITY_METADATA` | `PAYLOAD_TOMBSTONE` |

If a setting is unset, its location is left out of the registry; that is never an error. A config
file that cannot be parsed gives a `CONFIG_INVALID` location with no root. A category with no
location reports `status=NOT_CONFIGURED` and `bytes=0`. Herdr telemetry reports this way until a root
is injected. Sandbox roots are not discoverable from configuration, because the simulated adapter
deletes its temp workspace after every run, so they are injected the same way.

`account_storage(locations, policy=..., now=..., max_entries=...)` reports, for every
`StorageCategory`:

| Field | Meaning |
| --- | --- |
| `status` | `MEASURED` or `NOT_CONFIGURED` (per location also `MISSING`, `CONFIG_INVALID`, `ROOT_SYMLINK_REFUSED`) |
| `bytes`, `file_count` | totals over regular files (sizes from `lstat`) |
| `retention_classes` | classes of the catalog kinds of the category and of its locations |
| `oldest_mtime`, `newest_mtime` | ISO-8601 UTC |
| `eligible_for_purge` | `{bytes, items, basis, estimate_only: true, available}` |
| `truncated` | a walk hit `max_entries` (the numbers are lower bounds) |
| `locations` | location ids only |

The report carries `recommendation_only=true` and `autonomous_deletion=false`.

`eligible_for_purge` is an estimate. A location qualifies only if all of its kinds are R0/R1
`DELETE_RECORD`. A mixed R0/R1 location uses the longest window, which is the conservative choice
(`...:CONSERVATIVE_LONGEST_WINDOW`). R2 is never eligible (`NOT_ELIGIBLE_R2_HUMAN_DECISION_ONLY`).
Neither is R3 metadata (`NOT_ELIGIBLE_R3_AUTHORITY_METADATA`). The object store is never estimated as
eligible: a rejected payload needs authority-derived proof (`R3_PAYLOAD_PURGE_REQUIRES_EXPLICIT_REJECTION_PROOF`).

Remote staging is purged per record: the record's `state` decides its class, and `record.json`'s
mtime decides its age. Accounting reads sizes and mtimes only, so it cannot apply those rules. For
staging locations it therefore reports `available=false` with basis
`ESTIMATE_UNAVAILABLE_STAGED_RECORDS_USE_PLAN_PURGE`, and the category's `available` is false as
well. `plan_purge` gives the exact answer.

The walk (`pados.lifecycle.fswalk`) follows these safety rules:

- It never opens a file for reading; it reads sizes and mtimes only.
- It does not follow links. On POSIX each directory is opened with `O_DIRECTORY | O_NOFOLLOW`
  relative to its already-open parent, and each entry is `stat`ed with `dir_fd` and
  `follow_symlinks=False`. A directory that is swapped for a link after it was listed fails to open
  and is counted in `symlinks_skipped`. Links and Windows reparse points inside a root are never
  followed.
- **Root link policy:** a configured root whose final component is a link is refused
  (`ROOT_SYMLINK_REFUSED` in accounting, `PURGE_SCOPE_ROOT_IS_SYMLINK` / `OBJECT_ROOT_IS_SYMLINK` in
  purge). The root's parent components are trusted operator configuration.
- The budget is enforced while enumerating: once `max_entries` entries have been seen, enumeration
  stops (entries beyond the budget are never even `stat`ed) and `truncated=true` is set. Results are
  sorted only within the budget. Depth is capped at 64.
- Its output never includes a file name or path, only counts and bytes per category and location id.

`account_database(engine)` maps every table created in `migrations/` (both lanes, see
`pados.lifecycle.tables`) to a category, using `pg_total_relation_size` and `reltuples`. Tables it
cannot map are counted under `unmapped` but never named. If there is no engine or any
`SQLAlchemyError` occurs, it returns `status=DB_NOT_AVAILABLE`. DB row purge is not part of V1: every
DB category reports `eligible_for_purge.items=0` with basis `DB_ROW_PURGE_NOT_SUPPORTED_V1`.

`storage_report(settings, runtime_root=..., engine=None, include_database=False, ...)` is the single
entry point for CLI/API wiring.

## Purge semantics

### Rejection authority

A caller cannot assert that an artifact was rejected. `plan_purge` and `apply_purge` both ask a
`RejectionAuthority`, a read-only authority port with three methods:

- `rejection_evidence(hash)` returns a `RejectionEvidence`, or `None`.
- `referenced_by_accepted_state(hash)` fails closed: it returns `True` on any error.
- `refusal_code(hash)` gives the reason evidence is `None`, for reporting only.

`ProofRejectionAuthority(engine)` reads the P6.19B proof tables in a
`REPEATABLE READ, READ ONLY` transaction. It never writes. It is conservative: an object counts as
rejected and unreferenced only when all of the following hold.

- It is a `RESULT_PAYLOAD` object, and at least one result produced it (`output_object_id`).
- Every result that produces or references it ended in `FAILED` or `INVALID_SCHEMA`. A `SUCCEEDED`
  result may still be awaiting a human decision, so it is never treated as rejected.
- No `state_proof_commits` row exists for any of those results, whatever its status.
- No `MATERIALIZED` `state_proof_projections` row exists for any of those results.
- `p6_21h_product_read_index` does not reference the object.

Anything else, including any database error, gives `None`.

**What counts as a rejection.** This is a documented semantic, not an assumption. In PADOS a
`FAILED` or `INVALID_SCHEMA` result *is* the authoritative validation rejection:

- `StateProofService` records a result only after execution and validation have finished, and it
  records the final status at that point.
- It never commits or materializes a non-`SUCCEEDED` result (`commit_result` requires `SUCCEEDED`).
- Result rows are immutable: the trigger `state_proof_results_immutable` rejects UPDATE and DELETE.

**When the rejection happened.** Because the row cannot change after it is written, its
`created_at` is the instant its terminal status was decided; there is no later status transition.
`created_at == terminal decision instant` is therefore a PADOS invariant. It is guaranteed by
insert-once (the row is written only after its final status is known) together with the
immutability trigger. The authority relies on the invariant only after checking, in `pg_trigger`,
that the trigger really covers the queried table. It must be named `state_proof_results_immutable`
and be all of the following:

- on `state_proof_results` in the current schema;
- not an internal trigger;
- a `BEFORE`, `FOR EACH ROW` trigger;
- fired on both `UPDATE` and `DELETE` (`tgtype` bits 1, 2, 8 and 16);
- enabled in normal operation (`tgenabled` in `O` or `A`);
- executing `reject_state_proof_immutable_mutation()`.

I checked this query against a disposable PostgreSQL 16 built from the P6.19B migrations. It
returns evidence when the migration's trigger is present. It refuses with `REJECTION_TIME_UNKNOWN`
when the trigger is disabled or covers `UPDATE` only. It returns evidence again once the
`BEFORE UPDATE OR DELETE` row trigger is restored. The rejection instant is the latest `created_at` among the rejecting
results, normalized to UTC, and the retention window is measured between timezone-aware UTC
instants. If the trigger is missing, or any rejecting result has a naive or missing `created_at`,
the authority refuses with `REJECTION_TIME_UNKNOWN`.

The evidence names the latest rejecting result: `receipt_ref` is `state_proof_results:<id>` and
`decision_evidence_ref` is `state_proof_results:<id>:<status>`.

A rejection a human made on a `SUCCEEDED` candidate is not recognized in V1, so that candidate is
refused. Verifying the queries against a live proof database is part of the integrator's DB gate.

### Plan (`plan_purge`, dry run)

`plan_purge(scopes, now=..., policy=..., protected=..., candidates=..., object_root=...,
authority=...)` deletes nothing. If the filesystem state, the authority's answers and `now` are the
same, it returns the same `plan_hash`.

The hash binds:
- the retention windows (`r0_seconds`, `r1_seconds`, `explicit_override`);
- every item's identity: `(st_dev, st_ino, size, mtime_ns)`;
- for a staged record, its exact member manifest (each name and identity) and the directory's
  `(st_dev, st_ino)`;
- for a payload, the authority-derived evidence;
- every refused payload candidate with its reason.

How each kind of scope is planned:

- **`FILE` scope:** each regular file is one item.
- **`STAGED_RECORD` scope:** each remote staging record directory is one item.
  - Its class comes from `state` in `record.json` (`QUARANTINED` -> R1, `STAGED` -> R0), and its age
    from `record.json`'s mtime.
  - `record.json` is read through a no-follow descriptor with a 64 KiB hard cap, and its identity
    must match the listed member.
  - A record directory with more than 8 members, or with any link or non-regular member, is not a
    candidate.
- **Payload candidates:** `candidates` lists artifact hashes. Each is refused unless the authority
  yields evidence, the artifact is unreferenced, and `payload_retention` (R1) has elapsed since
  `decided_at`. Refusals are listed in `refused`, with these reasons:
  - `ARTIFACT_NOT_PROVEN_REJECTED`
  - `PAYLOAD_REFERENCED_BY_ACCEPTED_STATE`
  - `PAYLOAD_RETENTION_NOT_ELAPSED`
  - `PAYLOAD_NOT_PRESENT`

Scopes are refused in these cases:
- `PURGE_SCOPE_NOT_PURGEABLE`: the kind is not a non-authoritative R0/R1 `DELETE_RECORD`.
- `PURGE_SCOPE_OVERLAPS_PROTECTED`: the scope overlaps a protected root (R2/R3 locations, the object
  store, the tombstone ledger).
- `PURGE_SCOPES_OVERLAP`: the scope overlaps another scope.
- `PURGE_SCOPE_ROOT_IS_SYMLINK`: the root is a link.
- `PURGE_SCOPE_ROOT_NOT_DIRECTORY`: the root is a file.

`scopes_from_locations` splits the accounting registry into scopes and protected roots.

### Apply (`apply_purge`)

`apply_purge(plan, approved_plan_hash=..., now=..., authority=..., tombstones=...)` works in this
order:

1. **Approval.** It refuses unless `approved_plan_hash` equals `plan.plan_hash` (constant-time
   compare; `PLAN_HASH_NOT_APPROVED`). It also refuses a plan whose content no longer matches its
   hash (`PLAN_HASH_MISMATCH`).
2. **Policy.** It runs only under the plan's bound windows. A `policy` argument that differs from them
   is refused with `POLICY_MISMATCH`.
3. **Re-validation.** Every item is re-checked through descriptor-pinned, no-follow access. The
   object is unlinked relative to the pinned parent descriptor, so a parent directory swapped for a
   link cannot redirect the deletion. Per-item outcomes:
   - `REFUSED_OUTSIDE_ROOT`: a path component is a link, or the item is not plain.
   - `REFUSED_NOT_REGULAR`: the item is a link or a special file.
   - `SKIPPED_CHANGED`: the identity differs from the plan. A replaced file is caught even with its
     mtime restored, as is a staged record whose membership changed.
   - `SKIPPED_NOT_EXPIRED`: the item has not expired under the plan's windows.
   - `REFUSED_AUTHORITATIVE`: the kind is no longer R0/R1 `DELETE_RECORD`.
   - `PARTIAL_DIRECTORY_RETAINED`: an entry appeared in a staged record after its pre-check. Only
     approved members are deleted, so the directory and the new entry are kept.
   - `ALREADY_ABSENT`, `FAILED_IO`.
4. **Rejected payloads.** For each payload item:
   1. The authority is asked again. The planned evidence must be unchanged (`SKIPPED_CHANGED`), the
      artifact must not be referenced (`PAYLOAD_REFERENCED_BY_ACCEPTED_STATE`), and it must still
      be rejected (`REFUSED_NOT_REJECTED`). A tombstone that already exists never replaces this
      check.
   2. **The per-artifact lock is taken.** It is a process-safe lock (`flock` / `msvcrt.locking`) on
      `<object_root>/.pados-lifecycle-locks/<hash>.lock`, and it is held through step 7, from the
      pending lookup to the final tombstone. Concurrent purges and recoveries of one artifact
      serialize on it.
   3. The bytes must hash to the artifact hash, read through a no-follow descriptor whose identity
      matches the plan (`REFUSED_HASH_MISMATCH`). If a reserved name already exists, the item
      reports `PENDING_RECOVERY_REQUIRED`.
   4. A `PENDING` tombstone is appended. It records the reserved inode as `reserved_dev`,
      `reserved_ino`, `reserved_size` and `reserved_sha256`.
   5. **No-clobber reservation.** The verified inode is hard-linked as `.<hash>.purge-pending` in its
      pinned directory. If that name exists, the link fails, an `ABORTED` tombstone is written and
      the item reports `PENDING_RECOVERY_REQUIRED`. The canonical name is then removed with
      `unlink_verified`, so only if it is still the same inode, and the directory is fsynced. From
      here the canonical name no longer serves the bytes, and `ObjectStore.put_*` would publish them
      again.
   6. The authority is asked once more. If the artifact is now referenced or the evidence changed,
      the reservation is rolled back **without clobbering**:
      - The canonical name is restored by hard link from the reserved name, and the reserved name
        is removed (`REFUSED_REFERENCED_AFTER_RESERVE`).
      - If a writer has meanwhile re-published the canonical object, the link fails. The canonical
        object is kept if its bytes verify, and the reserved copy is dropped
        (`RESTORE_CANONICAL_ALREADY_PRESENT`).
      - If the canonical bytes do not verify, both are kept for review (`RESTORE_CONFLICT`).

      The restore runs under the artifact lock and is bound to the verified reserved inode:
      - Immediately before linking, the reserved name is re-opened without following links, and its
        identity and sha256 must still match.
      - After the link, a no-follow `stat` of the canonical name must show the verified `st_dev`/
        `st_ino`, and the hash of the opened descriptor must equal the artifact hash. Otherwise the
        name just published is withdrawn with `unlink_verified` and the result is `RESTORE_CONFLICT`.
      - If removing the reserved copy fails, the result is `RESTORE_CLEANUP_FAILED`.

      The canonical name is never overwritten. An `ABORTED` tombstone is written only when the
      restore fully succeeded. After `RESTORE_CONFLICT` or `RESTORE_CLEANUP_FAILED` the `PENDING`
      record stays, and no `ABORTED` or `COMPLETED` is ever recorded for a step that failed.
   7. Otherwise the reserved name is removed with `unlink_verified`, the directory is fsynced, and a
      `COMPLETED` tombstone is written (`PAYLOAD_PURGED`).
5. **Idempotence.** A second apply reports `ALREADY_ABSENT` or `ALREADY_PURGED` and changes nothing.
   A payload whose latest tombstone is `PENDING` reports `PENDING_RECOVERY_REQUIRED` and is not
   touched.

R3 metadata and R2 content are never touched. The proof DB, CAS and ScopeVersion are never written.

### Recovery (`recover_pending`)

`recover_pending(tombstones, object_root=..., authority=..., approved_plan_hash=..., now=...)`
resolves only the PENDING records of the approved plan. For any other plan it reports
`NOT_APPROVED`. Each record is handled under its per-artifact lock:

- **Re-check under the lock.** The latest record for the artifact is re-read under the lock. It must
  still be `PENDING`, its `plan_hash` must equal the approved hash (`hmac.compare_digest`), and its
  binding must equal the record validated before locking. The binding is the entry hash, schema,
  plan hash, artifact hash, evidence fields and reserved identity. If anything differs, the result is
  `RECOVERY_PLAN_MISMATCH` and nothing is touched. The recovery context is built only from that
  re-verified record.
- **Legacy records.** A legacy v2 `PENDING` has no bound reserved inode. It is reported as
  `LEGACY_PENDING_REQUIRES_OPERATOR` and never recovered automatically.

Recovery is bound to the reserved inode recorded in the PENDING tombstone. The reserved name is
opened with `O_NOFOLLOW` and its `fstat` (`st_dev`, `st_ino`, size) must equal the recorded
identity. The hash is computed from the opened descriptor. Any mismatch gives `RECOVERY_CONFLICT`
and nothing changes. The reserved copy is unlinked only with `unlink_verified`, and restores are
no-clobber, as in apply. Recovery works from what is on disk:

| On disk | Authority still valid | Action | Result |
| --- | --- | --- | --- |
| Reserved copy only | yes | Unlink the reserved copy. Write `COMPLETED`. | `RECOVERED_COMPLETED` |
| Reserved copy only | no | Restore by no-clobber link. Write `ABORTED`. | `RECOVERED_ABORTED` |
| Reserved copy and canonical (both hash-verified) | either | Keep the canonical. Drop the reserved copy. Write `ABORTED`. | `RECOVERED_ABORTED` |
| Canonical only | either | Write `ABORTED`. | `RECOVERED_ABORTED` |
| Neither | either | Write `COMPLETED`. | `RECOVERED_COMPLETED` |

A hash mismatch gives `RECOVERY_CONFLICT` and nothing changes. Recovery never deletes the canonical
object.

### Tombstones (two-phase, `METADATA_RETAINED_PAYLOAD_PURGED`)

`TombstoneLedger` is an append-only JSONL file. Each entry has the form
`{prev_hash, record, entry_hash}`, where `entry_hash` is the canonical hash of `{prev_hash, record}`
(the same chain pattern as `pados.remote.ledger.IntentLedger`). A record has exactly these keys:

`schema, phase, artifact_hash, data_kind, status, timestamp, receipt_ref, decision_evidence_ref,
recorded_at, purged_at, reason, payload_state, plan_hash, reserved_dev, reserved_ino,
reserved_size, reserved_sha256`

`reserved_sha256` must equal `artifact_hash`.

**Schema versions.** No released PADOS version wrote the earlier v2 schema, because Data Lifecycle
is new in this unreleased phase. Even so, well-formed v2 records (the same keys without the
`reserved_*` fields) are accepted as read-only history:

- The chain is still verified in full.
- A v2 record with any v3-only key, or with an inconsistent phase or payload state, is refused.
- `append` refuses v2 records (`TOMBSTONE_LEGACY_SCHEMA_READ_ONLY`). Every new write is v3 and
  continues the same chain.
- A legacy `PENDING` blocks apply for that artifact (`PENDING_RECOVERY_REQUIRED`). Recovery reports
  it as `LEGACY_PENDING_REQUIRES_OPERATOR`.

The phase determines the payload state:

| Phase | `payload_state` | `purged_at` |
| --- | --- | --- |
| `PENDING` | `PRESENT` | null |
| `COMPLETED` | `METADATA_RETAINED_PAYLOAD_PURGED` | equals `recorded_at` |
| `ABORTED` | `PRESENT` | null |

- Every read (`entries`, `latest`, `pending`) and every append first verifies the whole chain and
  every record. On any problem it fails closed with `TOMBSTONE_CHAIN_INVALID`, and apply reports
  `REFUSED_TOMBSTONE_CHAIN_INVALID` without touching the payload. A forged entry therefore cannot
  stand in for a real tombstone.
- Verify, append and fsync run under a process-safe exclusive lock on the sibling file
  `<ledger>.lock` (`fcntl.flock` on POSIX, `msvcrt.locking` on Windows). Separate instances or
  processes cannot fork the chain.
- Each append is fsynced. The parent directory is fsynced when the ledger file is created.
  `ensure_durable_dir` creates any missing ancestor directories (of the ledger and of the lock
  directory) one level at a time and fsyncs the parent of each one it creates. It refuses a target
  directory that is a link.
- Before it is written, each record is validated and then checked with
  `assert_minimized(record, allowed_keys=TOMBSTONE_KEYS, ...)`.
- A ledger path that is a link is refused.

If malicious rewriting of the whole ledger is in scope, its chain head needs an external anchor.
That is not part of V1.

### Residual risks (accepted for V1)

- **ACCEPTED_RISK_WINDOWS.** Windows has no descriptor-relative `open`/`unlink`/`link`. There,
  `fswalk` falls back to path operations that re-check each directory's real path and the file
  identity immediately before every mutation. A swap between that check and the system call
  remains possible, which also covers a directory swapped after `_checked()` in a walk. Directory
  fsync is a no-op on Windows. This is accepted on a single-user host. The tests run every purge
  scenario on both backends.
- **ACCEPTED_RISK: swap within a pinned directory (F06/F07).** On POSIX, `unlink_verified` opens
  the member with `O_NOFOLLOW | O_NONBLOCK`, requires its `fstat` identity to equal the planned
  identity, and unlinks the name only if a no-follow `stat` of the name still equals the open
  inode. A replacement in the instant between that last `stat` and `unlink` is not detected.
  Exploiting it requires already being able to write inside a PADOS-owned R0/R1 root on a
  single-user Home Base, and whatever is deleted stays inside that root.
- **ACCEPTED_RISK: acceptance during reservation (F04).** The per-artifact lock serializes
  Lifecycle, but object-store writers and acceptance do not take it. An acceptance that references
  the object between the final authority check and the unlink is not detected. Rollbacks never
  clobber a re-published object (N01). A complete fix needs acceptance to refuse objects that have
  a `.purge-pending` sibling, or to take the same lock. That lives in `pados.state_proof` and is
  outside Lifecycle.
- **ACCEPTED_RISK: unprotected chain head (F08).** Chain verification rejects corrupt or forged
  entries, but an attacker who can rewrite the whole ledger can recompute a consistent chain. The
  recommendation is an external anchor for the chain head (for example, the latest `entry_hash`
  recorded in the semantic audit chain or a signed checkpoint). Not in V1.

## Delete / revoke propagation

`plan_propagation(graph, source_id, reason=...)` calls `LineageGraph.plan_revocation`; it does not
duplicate the graph. It walks these stages, each with a status:

| Stage | Lineage kinds | Status when affected |
| --- | --- | --- |
| `SOURCE` | `SOURCE` | `PAYLOAD_PURGE_PLANNED` (identity hash retained) |
| `PROJECTION` | `NORMALIZED_ARTIFACT`, `RETRIEVAL_PROJECTION`, `GRAPH_PROJECTION` | `INVALIDATION_PLANNED` |
| `CHUNK` | `CHUNK` | `INVALIDATION_PLANNED` |
| `EMBEDDING` | `EMBEDDING` | `INVALIDATION_PLANNED` |
| `CACHE` | `CACHE` | `CACHE_INVALIDATION_PLANNED` |
| `DERIVED_ARTIFACT` | `EVALUATION_ARTIFACT` | `INVALIDATION_PLANNED` |
| `TRAINING_LINEAGE` | `DATASET_REFERENCE`, `TRAINED_ARTIFACT` | `HUMAN_REVIEW_REQUIRED` |

- `kind_status` gives a status for every `LineageKind`. `DATASET_REFERENCE` and `TRAINED_ARTIFACT` are
  always `HUMAN_REVIEW_REQUIRED`, and the plan says `model_weights_deleted=false` and
  `model_weight_deletion="UNSUPPORTED_REQUIRES_RETRAINING_DECISION"`.
- `cache_invalidation` lists the derived caches and projections to invalidate (`CACHE`,
  `RETRIEVAL_PROJECTION`, `GRAPH_PROJECTION`).
- `retained_metadata` keeps `SOURCE_IDENTITY`, `AUDIT_CHAIN` and `ARTIFACT_HASH` (R3) and marks the
  source payload `METADATA_RETAINED_PAYLOAD_PURGED`.
- `execution_order` is the reverse of the walk (leaves first), so no derived path keeps serving
  revoked data.
- `apply_propagation(graph, plan, approved_plan_hash=...)` requires the exact hash and re-plans to
  refuse a stale plan. It then delegates to `LineageGraph.apply_revocation`, which applies a
  projection mask and leaves human-review items untouched.

## Never persisted

`ProhibitedPersistence` lists what PADOS never persists by default: plaintext passwords, API keys,
the full reconstructed context, duplicated provider context, full terminal history, raw sandbox
scratch, temporary reasoning, raw HTTP bodies and duplicate uploads. Secrets are referenced only as
`secretref://<provider>/<path>`. Every new persistence point calls `assert_minimized` with an explicit
`allowed_keys` schema. In V1 that is the tombstone ledger.

## Persistence inventory and findings

`pados.lifecycle.inventory.INVENTORY` lists the existing writers. The tests in
`tests/lifecycle/test_minimization_inventory.py` exercise the real writers.

| Point | Writer | DataKind | Guard | May hold prohibited |
| --- | --- | --- | --- | --- |
| remote intent ledger | `RemoteOperationsService._record` -> `IntentLedger.append` | `REMOTE_INTENT_LEDGER` | structural | no (summary only) |
| remote staging | `StagingArea.stage` | `TEMPORARY_UPLOAD`, `QUARANTINED_UPLOAD` | structural | yes: duplicate uploads |
| compute-node probes | `NodeProber._store` | `MACHINE_CONNECTION_HISTORY` | structural | no (credential never stored) |
| connectivity snapshot | `ConnectivityMonitor.observe` | `CONNECTIVITY_HISTORY` | structural | no |
| product object store | `ObjectStore.put_json/put_bytes` | `ACTIVE_ARTIFACT`, `REJECTED_ARTIFACT_PAYLOAD` | none | yes: request context in run-context objects |
| release / test evidence | gate runners -> `runtime/artifacts/reports` | `RELEASE_TEST_EVIDENCE` | none | yes: command output |
| payload tombstones | `TombstoneLedger.append` | `PAYLOAD_TOMBSTONE` | `assert_minimized` | no |
| storage placement index | `PlacementIndexStore.save` | `STORAGE_PLACEMENT_INDEX` | `assert_minimized` | no (content-free) |

These findings are for the owning components. They are outside the lifecycle package and are not
changed here:

- **RESOLVED LEDGER_CONTEXT_FIELD_NAME** (`pados.remote.service.RemoteOperationsService._append`):
  SUBMITTED records keep `ReconstructedContext.summary()` under the key `context` (accepted replay and
  API shape). The value is now checked against the exact `CONTEXT_SUMMARY_KEYS` schema (fingerprints and
  counts only) and the rest of the record by `assert_minimized`.
- **RESOLVED LEDGER_UNGUARDED**: every ledger record passes the guard, and READ outcomes ledger run ids
  plus a digest instead of compact run rows (Security Hardening V1, F-19b).
- **CLAIM_RECORDS**: `IntentLedger.claim`/`release` write intent id, actor, request fingerprint and
  claim state before dispatch (cross-process duplicate guard); they pass `assert_minimized`.
- **STAGING_DUPLICATE_UPLOAD** (`pados.remote.staging.StagingArea.stage`): identical bytes staged
  twice are stored twice. There is no content-hash dedupe.
- **OBJECT_STORE_RUN_CONTEXT** (`pados.product_loop.service.ProductLoopService._run_context_payload`):
  the caller's `request.context` is persisted as `bounded_context` in a content-addressed object. The
  request schema bounds it, but it does not pass through `assert_minimized`.
- **EVIDENCE_COMMAND_LOGS** (release / acceptance gate runners): acceptance logs capture command
  output. They are R2 evidence, must be reviewed before release, and are never purged automatically.
- **CAPABILITY_ARTIFACT_CONTENT** (`pados.capability.artifacts.ObjectStoreArtifacts.put`, reached only
  through `ScopedArtifacts`): stage output bytes, such as OCR text or algorithm result envelopes, are project
  content written as given. Only `StageOutput` evidence is credential-checked. Final outputs are
  `DERIVED_DOCUMENT` (R2), intermediates are `CAPABILITY_INTERMEDIATE_ARTIFACT` (R0), and receipts and
  reproducibility records are `CAPABILITY_STAGE_EVIDENCE` (R1). All of them follow their source lineage on
  revocation.
- **PLACEMENT_INDEX_CONTENT_FREE** (`pados.lifecycle.storage_index.PlacementIndexStore.save`, Storage
  Flexibility V1+): the persisted placement index holds digests, sizes, artifact classes, tiers, backend ids
  and safe labels only, passes `assert_minimized` with an explicit schema, is written atomically (owner-only)
  and carries a digest of its own body. A mismatched digest refuses the load. The embedded digest detects
  accidental or naive tampering; for stronger evidence the caller keeps the digest elsewhere and passes it as
  the expected digest.

## Operator command

`pados storage report [--runtime-root runtime] [--database] [--json]` prints storage by category with the
`eligible_for_purge` estimate. It is read-only (`recommendation_only=true`, `autonomous_deletion=false`),
reports sizes and counts only (never file names), and `--database` adds per-category operational table
sizes. Purge is never run by this command; it requires an explicit plan (`plan_purge`) approved by its exact
plan hash (`apply_purge`).
