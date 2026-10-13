# Outcome Learning V1 (non-authoritative)

Package: `pados.learning` (`records`, `store`, `profiles`, `advisor`, `advice_log`, `anchor`).
Hardened per independent reviews CR-11 (OL-01 .. OL-07), CR-11b (OL-08 .. OL-10, OL-01/02/03/04/06/07
partials) and CR-11c (OL-11, OL-12, OL-02/03/04/07/08/09 partials).

Outcome Learning answers one question: *which capability / adapter / resource combination tends to work
best for which task context?* It is not autonomous reinforcement learning, and it never rewrites PADOS.
It cannot change authority, ACL, grants, semantic acceptance, provider authorization, security policy,
classification or human-approval requirements.

```
trusted PlanReceipt/StageReceipt + trusted plan context + governed verdict
                                                       (OutcomeAuthenticator, wired by the integrator)
        -> OutcomeRecord (closed enums, digests, assert_minimized)       records.py
        -> OutcomeStore (JSONL 0600, lock, R1 by execution time,
                         ingestion ledger, re-authenticated reads)       store.py
        -> CapabilityPerformanceProfile (deterministic, hashed)          profiles.py
        -> OutcomeAdvisor.advise -> Advice (bounded, explained, bound)   advisor.py
        -> AdviceLog (durable, sequenced, hash-chained, snapshots,
                      ancestry-proving PRUNED markers)                    advice_log.py
           every mutation validated against, then advanced by
           compare-and-set on, the ExternalAnchor (FileAnchor)           anchor.py
        -> OutcomeAdvisor.issued (anchored provenance, TTL, audit ref)   advisor.py
        -> seams.apply_advice (planner; feasible set only, same binding, clamped to +/-ADVICE_BOUND)
```

## Invariants

| Invariant | Where it is enforced |
| --- | --- |
| Ingestion is authenticated (OL-01) | `OutcomeAuthenticator.resolve_receipt(stage_receipt_hash)` must return the executor's own, structurally valid `PlanReceipt` containing that stage; `resolve_plan_context(plan_hash)` must return the trusted `TrustedPlanContext` (task type, classification, recorded context features); the record's `AUTHENTICATED_FIELDS` (execution facts, `task_ref`, `classification`, `executed_at`, `features_authenticated`) must equal those derived from them, and its human decision must equal `resolve_decision(plan.human_decision_ref)`. Otherwise `LEARNING_OUTCOME_UNAUTHENTICATED` (`OutcomeRecord.from_receipts`, `OutcomeStore.append`). Every read re-authenticates; records whose receipt, plan context or verdict changed are excluded and counted (`OutcomeStore.read_checked`). Resolver errors fail closed |
| Context features cannot steer a bucket (OL-01) | features recorded by the trusted plan context are authenticated (`features_authenticated=True`, must match exactly); stage-supplied features are kept only as unauthenticated and are never used for bucketing (`OutcomeRecord.bucket_features`): such a record falls into the feature-less bucket. Features conflicting with recorded trusted ones are refused (`CONTEXT_FEATURES_CONFLICT_WITH_TRUSTED_PLAN`) |
| Human decisions are never inferred | Only the governed verdict counts; a record carries `ACCEPTED`/`REJECTED` only with the opaque `dec-` digest of the plan's decision ref. Passing verification is not acceptance |
| No content, closed vocabularies (OL-04) | `failure_code` is the closed `FailureCode` enum (anything else -> `ADAPTER_FAILURE`); `hardware_class` is `HardwareClass`; `metric_name` is `MetricName`; resource ids, task types, decision refs and evaluator refs are stored only as `res-`/`task-`/`dec-`/`evr-` + 16 hex digests (keyed: see "Identity digests" below). Outcome records keep `capability_id`/`adapter_id`/`adapter_version` because they are authenticated against the trusted receipt (which binds the descriptor digests); every other persistence path stores identities only as `cap-`/`adp-` + 24 hex digests (`EvaluationRecord.capability_ref`/`adapter_ref`, advice-log `capability_ref`, candidate and snapshot `adapter_ref`, where `adp-` covers id AND version). Context features are the closed `CONTEXT_FEATURES` vocabulary; every record passes `assert_minimized` with an explicit `allowed_keys` schema |
| Insecure files are refused (OL-05) | every descriptor opened on a store, ledger or log file is `fstat`-checked: regular file, owned by the current uid, no group/other bits (POSIX), else `LEARNING_STORE_INSECURE` (never silently repaired) |
| Retention is physical and by execution time (OL-06, OL-10) | outcome records age from the trusted `executed_at` (the receipt's `finished_at`), never from `recorded_at`; an execution already outside the window is refused (`OUTCOME_RECORD_ALREADY_EXPIRED`). NO AUTONOMOUS DELETION (CR-11d OL-14): reads, advice issuance and appends never delete anything - expired and future-dated records/entries are HIDDEN from every read, and a full store or log REFUSES the next append (`OUTCOME_STORE_FULL_PRUNE_REQUIRED`, `ADVICE_LOG_FULL_PRUNE_REQUIRED`) instead of evicting. Physical deletion happens only through the operator-invoked `prune()`; the advice log's `prune()` removes expired, future-dated and over-capacity entries ANYWHERE in the log with a chained, anchored `PRUNED` marker. There is deliberately no timer or second scheduler (the lifecycle authority recommends; it never deletes autonomously) |
| One ingestion per receipt per window (OL-10) | the ingestion ledger (`<outcomes file>.ingest`, same lock and file checks) retains the outcome id (derived from the receipt hash) and execution time of every kept record inside the window; re-appending it is refused (`OUTCOME_ALREADY_INGESTED`). The ledger is bounded (`4 * max_records` identities); when full it REFUSES the append (`OUTCOME_INGESTION_LEDGER_FULL_PRUNE_REQUIRED`) and never folds or deletes identities on its own (CR-17); expired identities are removed only by the operator `prune()`. A floor written by an earlier version is still honoured: anything executed at or before it is refused (`OUTCOME_BELOW_INGESTION_FLOOR`) |
| Advice is bounded and bound (OL-03) | scores lie in `[-ADVICE_BOUND, ADVICE_BOUND]`; `advice_context_hash` binds task, capability, features, the candidate set (ids AND versions), the classification and the plan binding hash. Classification and plan binding are mandatory when enabled: otherwise the entry is logged `BINDING_INCOMPLETE`, no advice is returned, and `context_hash` returns an unbound hash that matches no advice. Old-version advice can never apply after an upgrade. The plan binding hash is the planner's `pados.capability.planner.plan_binding_hash`, which covers the fabric **task hash** (`TaskContract.task_hash`: task id and type, project, work contract, classification, network policy, required outputs, quality, risk, human gate, inputs and parameters), the template hash, the capability and the feasible candidate set: advice for one project / work contract / input set never applies to another (`test_cr11c_ol03_plan_binding_covers_project_and_work_contract`) |
| Validity period (OL-03) | `issued()` reports logged advice only within `AdvisorConfig.advice_ttl` of its `logged_at` (default 24 h; allowed 1 minute .. 7 days, else `ADVICE_TTL_OUT_OF_RANGE`); older or future-dated entries issue nothing (`None`). The TTL is an issuance property, not a score input, so it is not part of `config_hash` |
| Minimum evidence | no advice below `min_samples` (default 20, floor 5) observations of the same adapter version in the same (authenticated) context bucket |
| Disabled by default and reversible | `AdvisorConfig(enabled=False)` is the default; a disabled advisor returns no advice, which is exactly the no-advice plan |
| Durable, anchored audit (OL-02, OL-12) | an enabled advisor requires a file-backed `AdviceLog` (`ADVICE_LOG_DURABLE_REQUIRED`) **with a configured `ExternalAnchor`** (`ADVICE_ANCHOR_REQUIRED`, refused at construction). Design choice: enabling advice without an anchor would issue advice whose provenance cannot be established, so it is refused rather than reported as `UNANCHORED`. The only exception is the acknowledged test-only path `AdviceLog.ephemeral_for_tests(acknowledgement=TEST_ONLY_ACKNOWLEDGEMENT)`, which always carries its own in-memory compare-and-set anchor (`_TestMemoryAnchor`, referenced nowhere else; static AST tests prove no module under `src/` reaches either). Entries carry `seq`/`prev_hash`/`entry_hash`; advice is returned only after it is logged AND anchored. `advise_logged` returns the advice with the audit entry's `(entry_seq, entry_hash)` (`AdviceResult.audit_ref`) for the plan to record |
| Anchored issuance (OL-12) | `issued(plan_binding_hash, candidates=..., audit_ref=None)` returns advice only from `AdviceLog.anchored_view()`: under one lock the log (never modified by the read) is verified against `anchor.latest()` exactly like `verify()` - `ok`, `ANCHORED`, head equal to the anchored checkpoint - so every entry it can return lies within the anchored history. Rewritten, truncated, unanchored-suffix, unavailable-anchor or unverifiable history returns `None`. With `audit_ref=(seq, entry_hash)` (the pair the plan recorded) only exactly that entry counts; without it the latest entry for the binding. The planner's `AdviceRecord` does not carry the pair yet: a later fabric change should store `advise_logged(...).audit_ref` in it and pass it back as `issued(..., audit_ref=...)` (the extra keyword is optional, so `ScoreAdvisor` callers are unaffected) |
| Every mutation validated against the anchor (OL-11) | `record` and the operator `prune()` (the only mutations; reads never mutate) first load the log and check it against the anchor: the chain must verify, the anchored latest checkpoint must be retained with its hash (or proven through an anchored marker) AND be exactly the current head (`ADVICE_LOG_NOT_ANCHORED`, `ADVICE_LOG_HEAD_NOT_ANCHORED`, `ADVICE_LOG_TRUNCATED`, `ADVICE_LOG_REWRITTEN`, `ADVICE_LOG_MARKER_UNANCHORED` otherwise). Each mutation adds exactly one entry (a prune marker and an append are two transitions, each validated first), is written, and then advances the anchor by `record(previous=validated head, new)`. A refused (`ADVICE_LOG_ANCHOR_REFUSED`) or unreachable (`ADVICE_LOG_ANCHOR_UNAVAILABLE`) anchor restores the previous file content; the anchor keeps the trusted checkpoint, so a substituted chain is never laundered |
| Compare-and-set anchor (OL-11) | `ExternalAnchor.record(previous, new, *, marker=False)` refuses unless `previous` equals its latest (`ANCHOR_CONFLICT`) and `new.seq == previous.seq + 1` (`1` for the first; `ANCHOR_NOT_CONTINUING`); `latest()`; `markers()` (bounded history of anchored PRUNED markers). `FileAnchor(directory)` implements it: owner-only directory (0700, checked) with owner-only `anchor.head.json` and `anchor.initialized` (0600, `fstat`-checked), read-modify-write under `pados.lifecycle.locking.exclusive_lock`, atomic replace with file and directory fsync, sequence numbers and hashes only (`assert_minimized`). After initialization a missing head is `ANCHOR_HEAD_LOST`, a malformed one `ANCHOR_STATE_INVALID`, a group/other-accessible one `ANCHOR_STATE_INSECURE`; it is never re-created silently. Concurrent recorders retry on `ANCHOR_CONFLICT` and never regress the head |
| Anchored ancestry (OL-08) | every `PRUNED` marker records the chain head before the prune (`prior_head`, also its `prev_hash`), every removed run with the hash before it and the hash of its last removed entry (`ranges`), and the hashes committed by earlier markers (`ancestry`, newest 32 kept). Only the latest marker's ranges may bridge gaps, and every range must bridge one (`ADVICE_LOG_PRUNE_RANGE_INVALID`). Every marker is recorded in the anchor at prune time (`marker=True`) by compare-and-set from the anchored pre-prune head. `verify(checkpoint)` succeeds only when the anchored `(seq, entry_hash)` is a retained entry (`ANCHORED`) or is committed by the latest chained marker AND that marker is one the anchor recorded (`ANCHORED_THROUGH_MARKER`). A different hash is `ADVICE_LOG_REWRITTEN`; a fabricated marker - even one whose removed range ends exactly at a known anchored checkpoint - is `ADVICE_LOG_MARKER_UNANCHORED` (also without an anchor configured); anything else is the explicit `ADVICE_LOG_ANCHOR_UNVERIFIABLE`, never `OK`. `verify()` without a checkpoint uses `anchor.latest()`, requires the head to be that checkpoint (`ADVICE_LOG_HEAD_NOT_ANCHORED` for an unanchored suffix) and reports `ANCHORED`/`UNANCHORED` explicitly; an unreachable anchor fails writes and verification closed (`ADVICE_LOG_ANCHOR_UNAVAILABLE`) |
| Verifiable derivation (OL-07, OL-09) | each entry persists the configuration inputs (`advisor_version`, `min_samples`, `bucket_keys`) whose hash must equal `config_hash` (checked on parse and by `verify`). `OutcomeAdvisor.verify(entry)` rejects a configuration that differs from the verifier's (`ADVICE_CONFIG_DIFFERS`) unless `against_logged_config=True`, which is reported as `ConfigSource.LOGGED`; it checks status/shape, candidate uniqueness and canonical order, that keyed and unkeyed identity digests are not mixed (`ADVICE_IDENTITY_DIGESTS_MIXED`), the context hash recomputed from the logged digests, snapshot `min_samples`, the counter relations (`DerivationSnapshot.counters_consistent`: exactly the combinations some real outcome set produces - `passed <= verified <= n`, `accepted <= decided <= n`, `passed <= successes <= n`, `good <= min(successes, n - F, n - R)` and `good >= successes - min(F, successes - passed) - min(R, successes)` with `F = verified - passed`, `R = decided - accepted`; proven against exhaustive enumeration for `n <= 4`), the Wilson bound and the score. Passing all of that is `SCORE_ARITHMETIC_VERIFIED`; when the current records also reproduce the exact advice set (including candidates that received none) it is `DERIVATION_VERIFIED`. Historical entries whose records were pruned are verified from their snapshots (`SCORE_ARITHMETIC_VERIFIED`); `rederive` recomputes from current records only |
| No write path to authority | `pados.learning` imports nothing from policy, adapter execution, registry, grants/authorization, state_proof, product_loop, memory, worker (router), training, db or network modules, and no timer/scheduler module (`tests/learning/test_boundary.py`, `test_cr11b_learning.py`) |

## Records

`OutcomeRecord` (`pados.learning.outcome.v2`): `outcome_id` (from the receipt hash), `task_ref`,
`capability_id`, `adapter_id`, `adapter_version`, `resource_ref`, `hardware_class`, `network_state`
(`ConnectivityState`), `classification`, `context_features`, `features_authenticated`, `latency_ms`,
`cost_units`, `success`, `failure_code`, `verification` (`PASSED`/`FAILED`/`NOT_VERIFIED`),
`human_decision` (`ACCEPTED`/`REJECTED`/`NONE`) + `human_decision_ref` (digest), `quality_metric` in
`[0, 1]`, `retry_count`, `resource_usage` (counters), `plan_hash`, `receipt_hash`, `executed_at` (UTC,
trusted: the stage receipt's `finished_at`) and `recorded_at` (UTC ingestion time, informational; it may not
precede `executed_at` and never extends retention). Schema `pados.learning.outcome.v3`; v2 lines are
refused as `LEARNING_STORE_CORRUPT` (no silent migration).

Context features (closed vocabulary, at most 8 per record): `document_language` (`en`, `zh-Hant`,
`zh-Hans`, `ja`, ..., `mixed`, `unknown`), `scan_type`, `mime_class` (capability modalities),
`text_layer`, `page_bucket` (from `page_bucket(pages)`), `size_bucket` (from `size_bucket(bytes)`),
`layout`, `code_language`. Counts and sizes are recorded only as buckets.

`EvaluationRecord` (`pados.learning.evaluation.v3`): an offline benchmark result: `task_ref`,
`capability_ref` and `adapter_ref` (digests; `capability_ref_for` / `adapter_ref_for` map registry
identities to them), `dataset_snapshot_hash`, `dataset_size`, `metric_name` (closed), `metric_value`,
`higher_is_better`, context features, `evaluator_ref` (digest), optional `evidence_hash`. The id is
derived from (adapter version, snapshot, metric, context), so re-running a benchmark is idempotent.

### Production authenticator (integrator wiring)

`OutcomeAuthenticator` is a protocol. The integrator implements it over the fabric executor's receipt
store (`resolve_receipt`: stage receipt hash -> the executor's `PlanReceipt`), the planner's recorded plan
(`resolve_plan_context`: plan hash -> `TrustedPlanContext(task_type, classification, context_features)`,
where `context_features` is the advice context the plan recorded, or `None`) and the existing governed
approval path (`resolve_decision`: decision id -> `ACCEPTED`/`REJECTED`/`None`). The execution time is the
trusted stage receipt's `finished_at`; no separate channel is needed. `pados.learning` never ships an
in-memory authenticator; `tests/learning/learning_support.TrustedReceipts` is a test double.
Hardware class, network state, cost, quality and retries remain stage-supplied (see residual risks).

## Storage and retention

| Store | Retention | Pruning | DataKind |
| --- | --- | --- | --- |
| `OutcomeStore` | R1_OPERATIONAL (`RetentionPolicy.r1`, default 14 days) by `executed_at` | expired and future-dated lines hidden on reads and deleted only by the operator `prune()`; a full store refuses appends (`OUTCOME_STORE_FULL_PRUNE_REQUIRED`), never evicts | `OUTCOME_RECORD` |
| ingestion ledger (`<outcomes>.ingest`) | R1_OPERATIONAL by `executed_at` (outcome ids + execution times only) | expired identities deleted with the store's pruning; bounded with a rising floor | `OUTCOME_RECORD` |
| `EvaluationStore` | R2_PROJECT | never automatic; a full store refuses (`EVALUATION_STORE_FULL`); future-dated lines are not returned and are deleted only by the operator-invoked `prune()` | `EVALUATION_RECORD` |
| `AdviceLog` | R1_OPERATIONAL | expired and future-dated entries hidden on reads; removed (anywhere, plus over-capacity) with a chained, anchored `PRUNED` marker only by the operator `prune()`; a full log refuses appends; `verify`/`checkpoint` never modify | `ADVICE_LOG` |

Files are owner-only (0600) in an owner-only directory; links and insecure files are refused;
read-modify-write runs under the shared cross-process store lock (`pados.connectivity.storage.store_lock`).
A corrupt or foreign line fails closed with `LEARNING_STORE_CORRUPT`; it is never skipped or repaired.
Re-appending an identical record is a no-op; a different record under the same id is refused
(`LEARNING_RECORD_ID_CONFLICT`).

## Profiles

`build_profiles(records, bucket_keys=None, by_resource=False)` groups by
`(task_ref, capability_id, adapter_id, adapter_version, context bucket[, resource_ref])`. Profiles are
per adapter *version* so an upgraded engine does not inherit its predecessor's record.

Each profile reports: `sample_size`, `successes`, `success_rate`, `success_wilson_lower`,
`good_outcomes` (succeeded, verification not `FAILED`, human decision not `REJECTED`), `good_rate`,
`good_wilson_lower` (Wilson score interval, 95%), `verified` / `verification_pass_rate`,
`human_decided` / `human_accept_rate` (rates are `None`, not 0, without observations), nearest-rank
`latency_p50_ms` / `latency_p90_ms`, `cost_mean`, `quality_mean`, `retry_mean`, the time window and
`source_hash` (hash of the contributing record hashes). Aggregation is order-independent and rounded to
fixed precision; `profile_hash` is the canonical hash of the whole profile.

## Advice

Score (V1, reliability only): `score = ADVICE_BOUND * (2 * L - 1)` where `L` is `good_wilson_lower`.
Few observations give a conservative `L`. Latency and cost appear in the explanation and the profile but
do not move the V1 score.

The planner passes the same candidates, classification and plan binding hash to `advise` and to
`context_hash` (SEAM-01), and hands that hash to `seams.apply_advice`. All three are mandatory when advice
is enabled; a missing one yields `BINDING_INCOMPLETE` / an unbound hash, so nothing applies.

## Example: scanned Traditional-Chinese PDF -> OCR adapter A is better

Forty recent outcomes per engine for task `DOCUMENT_EXTRACTION`, capability `DOCUMENT_OCR`, context
`document_language=zh-Hant; mime_class=PDF; scan_type=SCANNED`:

| Adapter | Good outcomes | Wilson 95% lower bound | Advice score |
| --- | --- | --- | --- |
| `ocr.engine-a` v1 | 38 / 40 | 0.835 | +0.167 |
| `ocr.engine-b` v1 | 25 / 40 | 0.470 | -0.015 |

Explanation returned with engine A's advice (deterministic):

```
n=40>=min 20; good=38/40 (95.0%), wilson95_lb=0.835; verify_pass=95.0% of 40; human_accept=n/a of 0;
latency p50=1000ms p90=1000ms; cost_mean=1.000; score=+0.167 = 0.25*(2*lb-1); IDENTITY_DIGESTS_UNKEYED
```

If the planner's own base ranking preferred B by 0.10, the bounded adjustments (+0.167 / -0.015) move A
ahead. For a born-digital English PDF there are no observations in that bucket, so no advice is given
and the plan is unchanged. If a cloud OCR engine with a perfect record is denied by policy
(`NETWORK_NOT_PERMITTED` for a `LOCAL_ONLY` task), its advice is dropped by `apply_advice`; the policy
decision and its hash are unchanged.

```python
from pados.capability.seams import apply_advice
from pados.learning.advice_log import AdviceLog
from pados.learning.advisor import AdvisorConfig, OutcomeAdvisor
from pados.learning.anchor import FileAnchor
from pados.learning.store import OutcomeStore

anchor = FileAnchor(anchor_dir)  # OUTSIDE the advice log's writable store (or the audit authority's adapter)
advisor = OutcomeAdvisor(OutcomeStore(outcome_path, authenticator=receipt_authenticator),
                         config=AdvisorConfig(enabled=True), log=AdviceLog(advice_log_path, anchor=anchor))
features = {"document_language": "zh-Hant", "scan_type": "SCANNED", "mime_class": "PDF"}
pairs = tuple(sorted((a.adapter_id, a.version) for a in feasible_adapters))
result = advisor.advise_logged(task_type="DOCUMENT_EXTRACTION", capability_id="DOCUMENT_OCR",
                               candidates=feasible_adapters, context_features=features,
                               classification=task_classification.value, plan_binding_hash=plan_binding)
expected = advisor.context_hash(task_type="DOCUMENT_EXTRACTION", capability_id="DOCUMENT_OCR",
                                context_features=features, candidates=pairs,
                                classification=task_classification.value, plan_binding_hash=plan_binding)
adjustments = apply_advice([a.adapter_id for a in feasible_adapters], result.advice, context_hash=expected)
audit_ref = result.audit_ref  # (seq, entry_hash): record it in the applied plan
issued = advisor.issued(plan_binding, candidates=pairs, audit_ref=audit_ref)  # anchored, within TTL, exact entry
```

## Identity digests (OL-04)

By default the `res-`/`task-`/`dec-`/`evr-`/`cap-`/`adp-` digests are unkeyed SHA-256. They are opaque in
storage, but a guessable low-entropy label can be confirmed by dictionary hashing; this is reported as
`IDENTITY_DIGESTS_UNKEYED` in every explanation (`explain`, `logged_explanation`) and in
`AdviceVerification.identity_digests`.

An operator can configure a key held under a SecretRef:

```python
from pados.enterprise.secrets import SecretBoundary
from pados.learning.records import IdentityKey

key = IdentityKey.from_secret_ref(SecretBoundary(vault_resolver), "secretref://vault/pados/learning-id")
store = OutcomeStore(outcome_path, authenticator=receipt_authenticator, identity_key=key)
advisor = OutcomeAdvisor(store, config=AdvisorConfig(enabled=True), log=log, identity_key=key)
record = OutcomeRecord.from_receipts(plan, stage, authenticator=receipt_authenticator, ..., identity_key=key)
```

Every digest then becomes HMAC-SHA256 under that key with the same prefix and the scheme tag `k1`
(`task-k1-<16 hex>`, `adp-k1-<24 hex>`), reported as `IDENTITY_DIGESTS_KEYED`; without the key a
dictionary attack finds nothing (`test_cr11c_ol04_dictionary_attack_works_on_unkeyed_refs_only`). The key is
resolved once through `SecretBoundary` and lives only in memory: `IdentityKey` has no serialized form, a
redacted `repr` and refuses pickling; no store, log, anchor or explanation contains it. The same key must be
given to the record builder, the store and the advisor: records digested under another key (or none) fail
authentication in a keyed store and never match a keyed advisor (fail safe: no advice). Changing the key
starts a new digest space; existing records stop matching until they age out (R1).

## Residual risks and limits

* **The anchor is the trust root (OL-02/OL-08/OL-11).** Enabled advice requires an anchor; with it,
  substituting, rewriting, truncating or extending the log beyond the anchored head is detected before any
  mutation and before any issuance. The anchor must live outside the advice log's writable store: a writer
  able to modify BOTH the log and the anchor can still forge history (`FileAnchor` is only as strong as the
  separation of its directory; the recommended production anchor is an adapter over the existing audit
  authority). `ANCHORED_THROUGH_MARKER` proves that the anchor recorded the marker that commits to the
  pruned hash; it cannot prove that the pruned entries were removed only after expiry, because their
  timestamps are gone. A checkpoint strictly inside a removed run, or older than the 32 carried ancestry
  hashes, is reported `ADVICE_LOG_ANCHOR_UNVERIFIABLE`; markers older than the anchor's bounded marker
  history (`FileAnchor` default 64) are `ADVICE_LOG_MARKER_UNANCHORED`.
* **Crash between the log write and the anchor update (OL-11).** The log is written first and the anchor
  advanced second; a refused or failed anchor update restores the previous file. A process crash between
  the two leaves a head one entry beyond the anchored checkpoint, which fails closed
  (`ADVICE_LOG_HEAD_NOT_ANCHORED`) until an operator inspects the entry and truncates it. An unanchored
  suffix is deliberately never adopted automatically (that would launder an appended forgery). Likewise a
  crash during the first anchor initialization (sentinel written, head not) is `ANCHOR_HEAD_LOST` until an
  operator clears the empty anchor of an empty log. Removing both the log and the anchor resets history;
  that requires write access to both.
* **Stage-supplied descriptive fields (OL-01).** `hardware_class`, `network_state`, `cost_units`,
  `quality_metric`, `retry_count` and `resource_usage` are not in the receipts. They do not move the V1
  score or bucket a record (cost/latency appear in the explanation only), and there is one record per
  authentic receipt per window. Stage-supplied context features are stored but marked unauthenticated and
  never bucket; advice in a feature bucket therefore needs a plan context that records its features.
* **Unkeyed digests (OL-04).** Unless an `IdentityKey` is configured, digests are unkeyed and a guessable
  low-entropy label can be confirmed by dictionary hashing (reported as `IDENTITY_DIGESTS_UNKEYED`). Keyed
  digests protect labels only as long as the key does; digest helpers validate identifier syntax, not
  registry membership.
* **Store writers (OL-10).** The ingestion ledger stops replays through the API. A writer with direct
  file access can delete the ledger; it still cannot extend a record's life past its authenticated
  execution time, and every read re-authenticates the record.
* **Historical verification is snapshot arithmetic plus anchored provenance (OL-07, documented design
  limit).** After the outcome records are pruned, `verify` re-derives each score from its logged snapshot
  (`SCORE_ARITHMETIC_VERIFIED`: configuration, binding, counter realizability, Wilson bound and score), and
  the entry's provenance is established by the external anchor (the entry is part of, or committed by an
  anchored marker of, the anchored history). Together they prove that this advice was issued under this
  configuration and is arithmetically consistent with counters some real outcome set produces; they do
  NOT prove the counters themselves, the full profile (latency, cost, source hash) or why other candidates
  got no advice. Those are reproducible only while the authenticated records exist (`DERIVATION_VERIFIED`).
  Persisting the full per-outcome evidence would keep content-adjacent data beyond R1, so this limit is
  accepted by design.
* **Format change.** Outcome v3, evaluation v3, advisor v3 and the new marker layout are not backwards
  compatible: older lines fail closed as `LEARNING_STORE_CORRUPT` (the package had not been released). A
  durable advice log created before CR-11c has no anchor; with an anchor configured it is refused
  (`ADVICE_LOG_NOT_ANCHORED`) rather than adopted - start a new log.
* Exact-bucket matching only (no similarity smoothing between buckets); coarser buckets are an explicit
  `bucket_keys` choice.
* The advisor trusts the planner to pass the feasible set; `apply_advice` is the enforcement point.

## Recommended integrator changes (outside this package's allowlist)

* `pados.capability.planner._order` passes `task_type`, `capability_id`, `classification`,
  `network_policy` and `quality` as `context_features`. Those keys are not in the closed
  `CONTEXT_FEATURES` vocabulary, so `OutcomeAdvisor` logs `CONTEXT_INVALID` and gives no advice in the
  integrated planner. The planner should pass the stage's closed context features (e.g. `document_language`,
  `scan_type`, `mime_class`) and nothing else.
* The planner should call `advise_logged`, record `AdviceResult.audit_ref` (`(seq, entry_hash)`) in its
  `AdviceRecord`, and pass it back as `issued(binding, candidates=..., audit_ref=...)` on reproduction, so
  each applied plan is bound to exactly its audit entry (`pados.capability.planner`, a later fabric change).
* Wire `OutcomeAuthenticator` (receipt store, planner plan context, approval path) and an
  `ExternalAnchor` (an adapter over the audit authority implementing compare-and-set `record(previous,
  new, marker=...)`, `latest()`, `markers()`; `FileAnchor` in a separate owner-only directory otherwise).
* Configure an `IdentityKey` from a SecretRef for keyed identity digests (optional; see above).
* `src/pados/lifecycle/inventory.py` should list the ingestion ledger (`<outcomes>.ingest`, DataKind
  `OUTCOME_RECORD`, `Guard.ASSERT_MINIMIZED`) as a persistence point.
