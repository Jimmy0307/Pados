# Training Plane Foundation V1

Package: `src/pados/training/`. Tests: `tests/training/`.

**TRAINED != DEPLOYED. BETTER BENCHMARK != AUTHORIZED. TRAINING COMPLETE != PRODUCTION ACCEPTANCE.**

The Training Plane turns governed data into model artifacts that can be reproduced. Those artifacts are
candidates and nothing more. The package holds no authority: it never writes CAS, ScopeVersion, semantic
state, Memory or audit authority. It never ranks language-model providers either. `pados.worker.router.select_worker`
stays the only router. A trained model passes through four separate stages:

    installed != registered != authorized != selected != accepted

## Flow

```
DatasetSnapshot (immutable, content-addressed members)
        |
TrainingContract (kind, base model, snapshot refs, bounded config, backend, commit, seed, compute, licenses)
        |                                   contract_hash
TrainingJob  DRAFT --(signed APPROVE_TRAINING_RUN bound to run_approval_subject(job), anchored logs)--> APPROVED_TO_RUN
             APPROVED_TO_RUN --(explicit approval reference, re-checked in the consumed ledger)--> RUNNING
             RUNNING --> COMPLETED | FAILED | CANCELLED            (append-only, hash-chained event log)
        |
TrainingBackend.run  (scoped artifact facade: reads only snapshot members, writes one model artifact)
        |
EvaluationReport (held-out snapshot; disjoint members and records)  ->  BenchmarkComparison (advisory)
        |
ModelRegistryCandidate (artifact hash + metrics + evaluations + complete provenance)  candidate_hash
        |
CANDIDATE -> PROMOTION_REQUESTED -(PromotionGate: signed PROMOTE_MODEL bound to decision_subject, anchored)-> PROMOTED
          \-------------------------(signed REJECT_MODEL)----------------------------------------> REJECTED
        |
ModelDescriptorDraft (UNREGISTERED, UNAUTHORIZED): the operator registers it; a project grant must
name CAPABILITY:<capability_id> before policy can ever select it.
```

Every signed decision is produced on the operator side only (`operator_cli.main`, the future
`pados training decide`), from the approver's own OS login in an interactive terminal after typed
confirmation. Every authority-bearing transition requires an `ExternalAnchor` on the job log, the registry
log and the consumed-decision ledger.

## Contracts (`contracts.py`)

- `DatasetSnapshot` lists its members only as `ArtifactRef` values: SHA-256, size, media type and kind.
  `content_hash` is the canonical hash of the member manifest, which covers the schema reference, the record
  count and the ordered member list. A snapshot whose stored hash does not match the recomputed one is
  refused. `snapshot_hash` also binds the classification, license, provenance and `created_at`. Any member
  change produces a new content hash. Any metadata change produces a new snapshot hash.
- A `SnapshotRef` binds the snapshot id, the content hash and the snapshot hash. `TrainingContract`
  binds these:
  - the kind: `LORA_FINETUNE`, `EMBEDDING`, `RERANKER`, `VISION_DETECTION`, `ALGORITHM_FIT` or `EVALUATION_ONLY`
  - the base model, as a `ModelRef` that holds an identifier, a version, an optional artifact hash and a
    license (never a URL or a path)
  - the training and held-out snapshot refs
  - a bounded config: at most 64 scalar entries with identifier keys and no credential-shaped values
  - the backend adapter id
  - the code version, which must be a git commit
  - the seed
  - the compute resource reference
  - the classification ceiling
  - the `LicenseConstraints`
- `check_snapshot` and `check_contract` re-validate objects that were handed over, because `model_copy`
  bypasses construction checks. `validate_contract` runs the checks that do not depend on the backend:
  - the snapshots match the contract exactly
  - the evaluation split is held out (no shared members)
  - the classification is within the contract ceiling
  - the dataset, backend and base-model licenses are on the allowed lists
  - the contract names this backend

## Jobs (`jobs.py`)

- The event log is the only job state. Each entry holds only these fields: `seq, job_id, event, from_state,
  to_state, contract_hash, kind, backend_adapter_id, dataset_hash, approval_id, decision, artifact_sha256,
  metrics_hash, failure_code, at, prev_hash, entry_hash`. `decision` is set only on the APPROVED event and
  holds the full signed run approval (all bound fields and the MAC, never key material). Every entry passes
  `assert_minimized` with that explicit schema, so dataset content is never written to the log.
- Replay rejects a broken chain, an unknown event and any illegal transition. A re-chained forgery that tries
  to move a job to `PROMOTED` or any other state outside the job vocabulary is refused with
  `JOB_LOG_CORRUPT`. `transition()` refuses `PROMOTED`, `REGISTERED`, `DEPLOYED` and similar states with
  `JOB_STATE_UNKNOWN`.
- `approve` and `start` require the job log, the consumed ledger and the registry log to be anchored
  (`ANCHOR_REQUIRED`, CR-12c TP-01); see "External anchor" below.
- `approve` consumes a signed `APPROVE_TRAINING_RUN` decision bound to exactly one job (CR-12b TP-01a). Its
  subject is `run_approval_subject(job)`: the canonical hash of the contract hash, the job id and the hash of
  the job's `CREATED` event. `TrainingJobs.approval_subject(job_id)` returns it for the operator tool. An
  approval therefore never applies to another job with the same contract, nor to a job re-created under the
  same id after a rollback (`DECISION_SUBJECT_MISMATCH`). `approve` runs only after the state check, so a
  refused move does not use up a decision. It is idempotent: the same decision replayed for the same job
  records nothing. A decision id that the job log or the registry log already names is refused for any other
  job (`DECISION_ALREADY_USED`).
- `start` requires the explicit approval id. `approval_verified` re-verifies the retained decision: MAC,
  operation, subject (this exact job), registered approver, and that the APPROVED time lies inside the
  signed window. It also checks that the consumed ledger holds the same record, that the job it is given
  equals the job replayed now from the log, and that all three logs sit exactly at their anchored heads.
  Over unanchored logs it is always `False`. Otherwise the start is refused with `RUN_APPROVAL_UNVERIFIED`
  (or the anchor code). A genuine APPROVED event transplanted into another job's history does not verify.
- `TrainingJobs` receives a verify-only `DecisionVerifier`. It refuses any object that can sign.
- `execute_job` runs the steps in this order:
  1. Validate the contract against the backend.
  2. Check that the backend is available.
  3. Start the job.
  4. Run the backend through a `ScopedArtifacts` facade that can read only the snapshot members and write
     one output of at most 16 MiB.
  5. Record `COMPLETED` (artifact hash and metrics hash) or `FAILED` (closed code).

  An output that was not written through the scope fails closed.
- `jobs.py` cannot import the registry or the promotion gate. This is enforced by a static test.

## Backends (`backends.py`)

- `AlgorithmFitBackend` (`training.algorithm-fit`) is the only backend that runs in V1. It is pure Python,
  local only, PADOS-owned and licensed AGPL-3.0-only (the PADOS Core license). It fits on a `training.tabular/v1` dataset: JSONL records
  of the form `{"features": [...], "label": x}`. Two methods are available:
  - `OLS`: least squares with an optional ridge `l2` and an unpenalized intercept, using normal equations
    and Gaussian elimination with partial pivoting. Singular systems are refused.
  - `LOGISTIC`: binary logistic regression, trained by full-batch gradient descent from a seeded
    initialization.

  Work is bounded to records × features × iterations ≤ 5·10⁷, at most 100 000 records and at most 64
  features. The model artifact is canonical JSON with these fields: algorithm, coefficients rounded to 12
  significant digits, contract hash, dataset hash, config hash, seed and backend id. The same contract and
  seed give the same artifact hash.
- `ContractOnlyBackend` records each future backend's contract: kind, adapter id, required config keys,
  accepted dataset schema references, resources, and engine license with `Distribution.RUNTIME_OPTIONAL`.
  `available()` is always `False` and `run` raises `BACKEND_UNAVAILABLE`.

  | Kind | Adapter id | Engine concept (public) | License recorded | Required config |
  | --- | --- | --- | --- | --- |
  | LORA_FINETUNE | training.lora-finetune | LoRA (Hu et al., 2021), PEFT | Apache-2.0 | rank, alpha, learning_rate, epochs |
  | EMBEDDING | training.embedding | contrastive bi-encoder | Apache-2.0 | epochs, batch_size, learning_rate |
  | RERANKER | training.reranker | cross-encoder relevance | Apache-2.0 | epochs, batch_size, learning_rate |
  | VISION_DETECTION | training.vision-detection | single-stage detector (YOLO family) | AGPL-3.0-only | epochs, image_size, batch_size |
  | EVALUATION_ONLY | training.evaluation-only | generic evaluation job (PADOS-owned) | AGPL-3.0-only | metric |

  The detection engine's copyleft license is recorded deliberately. A contract whose `allowed_backend_spdx`
  does not list it is refused with `BACKEND_LICENSE_NOT_ALLOWED`.
- `resolve_backend` matches the exact adapter id and kind only. There is no fallback to another backend,
  kind or engine.

## Evaluation (`evaluation.py`)

- `evaluate_algorithm_fit` refuses an evaluation snapshot that:
  - has the same content hash as the training snapshot
  - shares a member artifact with it
  - shares any canonical record with it (`EVALUATION_RECORDS_OVERLAP_TRAINING`)
  - was evaluated against a model trained on a different dataset

  The metrics are mse, r2 and records for OLS, and log_loss, accuracy and records for LOGISTIC.
- An `EvaluationReport` binds the model artifact hash, the held-out snapshot ref and the training dataset
  hash. Its `semantic_authority` is always `NONE`.
- `BenchmarkComparison` compares two reports on the same held-out content hash and a known metric
  direction. It is advisory only: `authorizes` can never be `True`.

## Registry and promotion (`registry.py`, `decisions.py`)

- `ModelRegistryCandidate` binds the artifact, the training metrics, the evaluations and the `Provenance`
  into `candidate_hash`. Every evaluation must be for this artifact and this training dataset.
  `Provenance` has these fields, all required:
  - job id
  - contract id and hash
  - kind
  - snapshot id, content hash and snapshot hash
  - classification
  - dataset, backend and output licenses
  - config hash
  - base model (required for every kind except `ALGORITHM_FIT`)
  - backend id
  - code commit
  - seed
  - compute reference
- `ModelRegistry.register` trusts nothing that the caller presents (CR-12 TP-03, TP-04). It checks these
  conditions:
  - the job is `COMPLETED` for this artifact, and its retained run approval still verifies
  - the metrics hash equals the hash the job recorded
  - the full contract-to-snapshot binding holds, including the snapshot hash that covers classification,
    license and provenance metadata, and the designated evaluation snapshot matches the contract
  - the backend license comes from the trusted, operator-configured backend registry (`backends`), never
    from the caller, and every license constraint is re-checked
  - the provenance re-derived from the contract, the snapshot and the job event log matches exactly
  - for `ALGORITHM_FIT`, the artifact read from the trusted store names this contract and dataset
  - every presented evaluation report equals a recomputation by the trusted evaluator over the contract's
    designated evaluation snapshot (`evidence_hash`; actual member and record disjointness is checked).
    Evaluations for other kinds are refused with `PROMOTION_EVALUATION_UNVERIFIABLE` in V1.

  `build_candidate` computes the evaluation itself and never accepts reports from the caller.
  `request_promotion` requires at least one registry-verified held-out evaluation (`ALGORITHM_FIT` only). It
  decides nothing.
- `PromotionGate.decide` is the only path to `PROMOTED` or `REJECTED`. It takes no metrics or benchmark
  input. It consumes a signed `PROMOTE_MODEL` or `REJECT_MODEL` decision bound to the exact registry entry
  (CR-12b TP-01a): `decision_subject(record)` hashes the candidate hash, the candidate id and the hash of the
  candidate's `REGISTERED` event; `ModelRegistry.decision_subject(candidate_hash)` returns it for the
  operator tool. A candidate whose content changes has a new hash, another candidate with an identical
  artifact has another id and registration event, and a candidate re-registered after a rollback has another
  registration event, so the old decision applies to none of them.
- Signing and verification are separate (CR-12 TP-02). `pados.training.operator.DecisionSigner` is the only
  object with `issue()`. It is opened through `open_operator_signer(approver=..., boundary=...,
  registry=...)`. Jobs and the registry receive a `DecisionVerifier`, which has no signing method. A static
  AST test and a runtime type check refuse a signer on the training side. No production constructor accepts
  an environment override; the process environment is read through a private test seam only.
- Operator-process boundary (CR-12c TP-02). `pados.training.operator` is imported only by
  `pados.training.operator_cli`, whose `main(argv)` is what the future `pados training decide` command
  calls (the CLI itself is outside this package's allowlist). No training service module (`jobs`,
  `registry`, `evaluation`, `backends`, `contracts`, `decisions`, `_chain`) imports `operator` or
  `operator_cli`; a static AST test resolves absolute, relative and aliased imports over all of
  `src/pados` to enforce it. `main` loads the operator registry, opens the signer, resolves the approver's
  key from the operator's own environment (`secretref://env/<NAME>` through `SecretBoundary`), asks for
  confirmation and writes the signed record to a new file (`O_EXCL`, mode 0600, `assert_minimized`). It
  exits `2` with the refusal code on stderr otherwise.
- Authenticated operator session (CR-12c TP-02). Every signature, checked when the signer is opened and
  again inside `issue` (so constructing `DecisionSigner` directly bypasses nothing), requires:
  1. no CI or automation marker (`AUTOMATION_CANNOT_DECIDE`), and a registered, enabled approver;
  2. the process runs as the OS account the operator registry binds to that approver
     (`OPERATOR_SESSION_MISMATCH`). On POSIX this is the numeric uid from `os.getuid()` plus the login name
     from `pwd`. On Windows it is `getpass.getuser()` together with the `USERNAME` and `USERDOMAIN`
     environment variables: best-effort, **PENDING Windows validation**;
  3. stdin and stdout are terminals (`OPERATOR_SESSION_NOT_INTERACTIVE`);
  4. the human types, on the controlling terminal (`/dev/tty`, or `CONIN$` on Windows, never a redirected
     stdin), the operation name and the first 12 hex characters of the subject hash, for example
     `PROMOTE_MODEL 0123456789ab` (`OPERATOR_CONFIRMATION_MISMATCH` for anything else; whitespace is
     normalized, case is not).

  The OS session and the terminal reader are private seams (`operator._current_session`,
  `operator._read_confirmation`) that only tests replace.

  **What this authenticates:** the decision was signed from the approver's own OS login session, in an
  interactive terminal, after someone at that terminal typed a confirmation that names the operation and
  the subject. **What it does not authenticate:** an attacker who already runs code as that same OS account
  and controls its terminal (or a pseudo-terminal it allocates) passes every check. It does not prove which
  human is at the keyboard. It does not remove the symmetric-key residual below: code that can resolve the
  approver's SecretRef can compute the HMAC without this module.
- Per-approver keys and accounts (CR-12b TP-02a, CR-12c TP-02). The operator registry
  (`pados.training-operators/v3`) lists each human approver with their own key reference and their own OS
  account: `{"operator_id", "enabled", "key_ref": "secretref://...", "os_account": {"platform": "posix",
  "uid": <int>, "login": "<name>"}}` (or `{"platform": "windows", "domain": "<domain>", "login": "<name>"}`).
  Automation principals, duplicate principals, shared key references, shared OS accounts and privileged
  accounts (uid 0, `root`, `SYSTEM`, `Administrator`) are refused. The `v1` and `v2` schemas are refused.
  A signer is bound to one approver and resolves only that approver's registered key through
  `SecretBoundary`, when it is opened (`DECISION_KEY_UNAVAILABLE` if the caller cannot resolve it) and
  again for each signature; `issue()` takes no approver argument. The verifier checks a record only under the key registered for the approver it
  names, so approver A's key cannot sign as approver B (`DECISION_SIGNATURE_INVALID`) and an unknown or
  disabled approver is refused (`APPROVER_NOT_REGISTERED`).
- Verify-only (CR-12b TP-02b). `DecisionVerifier` holds the `SecretBoundary`, the operator registry (key
  references) and the consumed ledger, never resolved key bytes. Its public surface is `verify(record,
  binding, at=...)`, `check`, `consume` and `consumed_matches`, which return outcomes only, plus
  `anchoring`. The one verification-side MAC computation is `decisions._signature_matches`: it resolves the
  approver's key for a single constant-time comparison and returns a boolean. A static AST test enforces
  that no training module other than `operator.py` computes an HMAC anywhere else.
- Decisions follow the concept of the release approval authority. They are implemented locally;
  `pados.release` is not imported or modified. A decision works as follows:
  - It is signed with HMAC-SHA256 under the approver's own key. The key is referenced by SecretRef and
    resolved through `SecretBoundary`. The record schema is `pados.training-decision/v2`.
  - It binds the operation, the subject hash, a registered human approver, `origin = HUMAN_OPERATOR`, and a
    validity window of at most 24 hours.
  - It is single-use, enforced by a hash-chained consumed-decision ledger.
  - It is refused when CI or automation markers are present in the environment or in the record.
  - Automation principals cannot be registered or sign. This covers `training-*`, `job-*`, `pados-*`,
    `pipeline*`, `scheduler*`, `worker-*`, `service-*`, `ci-*` and bots. As a result, a training job cannot
    approve its own promotion.
- The full signed record is retained in each `PROMOTED` or `REJECTED` registry entry and in the consumed
  ledger entry (CR-12 TP-01). `PromotionGate.verified_state` returns a `GateVerdict(state, anchoring)`. It
  re-verifies the retained decision: MAC, operation, subject (this exact registry entry), registered approver
  equal to the recorded actor, and that the transition time lies inside the signed window. It also checks
  that the consumed ledger holds the same record. Any decided entry that fails is reported as `UNVERIFIED`,
  and `descriptor_draft` refuses it with `DECISION_UNVERIFIED`. Forging or transplanting a decided transition
  therefore requires the approver's key, even for someone who can rewrite every ledger.
- Decisions are idempotent per (decision id, registry entry): replaying the same decision for the same
  candidate records nothing. A decision id already named by the registry log or the job log is refused for
  any other transition.
- External anchor (CR-12b TP-01b; mandatory for authority since CR-12c TP-01). `TrainingJobs`,
  `ModelRegistry` and `ConsumedDecisionLedger` accept `anchor: ExternalAnchor`, a protocol with
  `bind(log_name, log_id)`, `record(log_name, seq, head_hash)` and `latest(log_name)`. The same anchor is
  configured for all three logs (the job side checks the registry log against it under
  `registry_log_name`).
  - **Mandatory.** `approve`, `start`, `register`, `PromotionGate.decide` (PROMOTE_MODEL and REJECT_MODEL)
    and `descriptor_draft` refuse with `ANCHOR_REQUIRED` unless the job log, the registry log and the
    consumed ledger are all anchored. Unanchored logs can still be read for reporting: `verified_state`
    returns `GateVerdict(..., UNANCHORED)` and reports every decided entry as `UNVERIFIED`.
  - **Accepted head == anchored head.** Every read of an anchored log requires its head to equal the
    anchored head. A log that is shorter than the anchored head, or diverges below it, fails with
    `LEDGER_ROLLBACK_DETECTED`. A log that is ahead of it fails with `LEDGER_HEAD_NOT_ANCHORED`. The
    authority-bearing reads (`verified_state`, `descriptor_draft`, `approval_verified`, `start`,
    `register`, `approve`, `decide`) check all three logs this way (`ModelRegistry.verify_heads`,
    `TrainingJobs.verify_heads`). A coordinated rollback of every log with the anchor left intact,
    including the restore of an already-consumed historical promotion, is therefore always detected, and
    historical verification never accepts a restored decided entry.
  - **Interrupted appends (CR-12c-01).** An entry is made durable before its head is recorded. If
    recording fails, the append raises `LEDGER_ANCHOR_UNAVAILABLE` and the entry is the log object's one
    outstanding head. The next read through that same object, under the log lock, records it first and
    accepts it only once recorded (a retry therefore completes); while the anchor is still unavailable it
    fails with `LEDGER_HEAD_NOT_ANCHORED`, and so does every other reader. An unanchored suffix never
    grows past one entry, and two or more unanchored entries are never recovered. After a crash there is
    deliberately NO in-process recovery API (CR-12d-01, integrator mitigation after the third fixer cycle):
    an unanchored head cannot be blessed from caller input. The log stays fail-closed
    (`LEDGER_HEAD_NOT_ANCHORED`) until the operator, offline, restores it to the anchored head from backup
    or retires it (new log, new anchor name) and re-issues any lost decision.
  - **`FileExternalAnchor(directory)` (CR-12c-02).** The operator places the directory on storage the
    training process cannot rewrite (another mount, another account, WORM storage). It is refused inside
    or around the log directory (`ANCHOR_INSIDE_LOG_STORE`). Per log name it keeps:
    - `<log_name>.anchor.init`, written once (`O_EXCL`, fsync, directory fsync) when the log is first
      bound. It records the log identity, `log_id`, which is a hash of the resolved log path and never
      the path itself;
    - `<log_name>.anchor.json`, the current head, starting at the genesis head `seq = 0`;
    - `.<log_name>.anchor.lock`.

    `bind`, `record` and `latest` run under that per-name `pados.lifecycle.locking.exclusive_lock`, so
    concurrent recorders never regress the head. Head writes are atomic (temporary file, fsync,
    `os.replace`, then a directory fsync on POSIX; Windows has no directory fsync). A head that vanished
    after initialization fails with `LEDGER_ANCHOR_LOST` and is never re-initialized silently. A head
    without its initialization record, or any unreadable, symlinked or malformed file, fails with
    `LEDGER_ANCHOR_UNAVAILABLE`. A second log claiming a name already bound to another log identity fails
    with `ANCHOR_LOG_NAME_IN_USE`. This holds in-process and, through the init record, across processes,
    so distinct ledgers always have distinct anchor identities.
  - `GateVerdict.anchoring`, `PromotionGate.anchoring`, `TrainingJobs.anchoring` and
    `ModelDescriptorDraft.anchoring` say `ANCHORED` only when the registry log, the job log and the
    consumed ledger are all anchored (for `TrainingJobs`: its log, the ledger and a configured
    `registry_log`), and `UNANCHORED` otherwise.
- `descriptor_draft` yields a `ModelDescriptorDraft` (which carries `anchoring`) whose `AdapterDescriptor` proposal is `EXPERIMENTAL`,
  `LOCAL_ONLY`, classified at the dataset classification and licensed under the contract's output license.
  The draft is marked `UNREGISTERED` and `UNAUTHORIZED`. The capability policy refuses it with
  `NOT_AUTHORIZED` until the operator registers it and a project grant names `CAPABILITY:<capability_id>`.

## Persistence and retention

| Writer | File | Fields | DataKind |
| --- | --- | --- | --- |
| `TrainingJobs` | operator-chosen JSONL | `JOB_LOG_FIELDS` | `TRAINING_JOB_EVENT_LOG` (R2) |
| `ModelRegistry` | operator-chosen JSONL | `REGISTRY_FIELDS` | `MODEL_REGISTRY_LEDGER` (R3) |
| `ConsumedDecisionLedger` | operator-chosen JSONL | `LEDGER_FIELDS` | `TRAINING_DECISION_LEDGER` (R3) |
| `FileExternalAnchor` (required for authority) | operator-chosen directory outside the log store, `<log_name>.anchor.json` | `schema, log_name, seq, head_hash` | head of the anchored ledger (same kind as that ledger) |
| `FileExternalAnchor` | same directory, `<log_name>.anchor.init` (written once) | `schema, log_name, log_id` (hash of the log path) | same kind as that ledger |
| `operator_cli.main` (operator side) | operator-chosen new file | the signed decision record (`RECORD_FIELDS`, MAC, no key) | handed to the training side, which retains it in the R3 ledgers |
| model artifacts | `ObjectStoreArtifacts` (content-addressed) | canonical model JSON | `ACTIVE_ARTIFACT` once accepted |

All three logs append under an exclusive file lock (fcntl, or msvcrt on Windows). They use `O_NOFOLLOW`,
refuse symlinks and verify the chain on every read. Each entry holds only identifiers, hashes, states, counts,
timestamps and, for authority-bearing transitions, the retained signed decision record.

## Residual risks

- **`ASYMMETRIC_SIGNATURE_UPGRADE = USER_DECISION_REQUIRED`.** Decisions use HMAC, which is symmetric. Per-
  approver keys mean a valid MAC proves possession of that approver's key, and the verifier retains no key
  bytes and offers no MAC computation. However, any in-process code that holds the same `SecretBoundary` can
  still resolve an approver's SecretRef and sign as that approver; this is the platform's operator-held
  symmetric model, which the frozen deployment-approval primitive shares. Closing it fully needs asymmetric
  signatures (for example Ed25519 verification, with the private key held only by the operator tool). That
  requires a new cryptography dependency and alignment with the frozen governance primitives, so the
  decision belongs to the user and is out of scope for V1. The verifier interface (`verify`, `check`,
  `consume`) would stay unchanged.
- **Coordinated rollback.** Closed for authority (CR-12c TP-01): unanchored logs cannot approve, start,
  register, promote or reject (`ANCHOR_REQUIRED`), and over anchored logs any rollback below the anchored
  head fails closed. If the attacker can ALSO rewrite the anchor store, a rollback of logs and anchor
  together can at most let a still-valid decision re-apply the identical human-approved transition to the
  identical job or registry entry inside its signed window (at most 24 hours). It cannot fabricate a
  decision, retarget one or apply one after expiry (tested with `anchor_too`).
- **Anchor scope.** The anchor protects only what is recorded in it. An attacker who can write the anchor
  store defeats rollback detection, and an anchor configured after the logs already hold entries fails
  closed (`LEDGER_HEAD_NOT_ANCHORED`); V1 has no adoption path for pre-existing unanchored logs. Deleting
  both the init record and the head file of a never-appended log is indistinguishable from "never bound".
  A crash between writing the init record and the genesis head leaves `LEDGER_ANCHOR_LOST`, which needs
  operator cleanup (fail closed).
- **Interrupted transitions burn the decision.** When the consumed-ledger append succeeds but its anchor
  record fails, the decision is consumed although the job or registry transition was not recorded. A retry
  fails with `DECISION_ALREADY_CONSUMED`, and the approver must sign a new decision (fail closed, tested).
- **Log-store writers.** An entry appended by someone with only log-file access is never anchored on
  their behalf: it is rejected with `LEDGER_HEAD_NOT_ANCHORED`. The explicit operator recovery is the one
  exception, for a single entry the operator inspected. Code running inside the training process can
  call the anchor itself; the anchor's guarantee is against rollback, not against in-process code.
- **Key rotation.** A retained decision is re-verified under the approver's currently registered key. After
  rotating or removing that key, earlier decided entries report `UNVERIFIED` (fail closed) until the operator
  re-decides.
- **Approver identity (CR-12c TP-02).** Signing is bound to the approver's OS login session and to an
  interactive, typed confirmation. It is not bound to a particular human: anyone (or any code) acting as
  that OS account with control of its terminal can sign. The Windows binding relies on environment-derived
  names and stays best-effort until validated on Windows. The verifier cannot check any of this after the
  fact: the record carries no session evidence, only the MAC. The verifier-can-resolve-the-symmetric-key
  part remains `ASYMMETRIC_SIGNATURE_UPGRADE = USER_DECISION_REQUIRED`.

## Rules for future backends

A real LoRA, embedding, reranker or detection backend must meet every rule below:

1. Implement `TrainingBackend`. It must keep the adapter id, kind, required config keys, schema
   references and recorded license of its contract-only spec. Any change to these is a new, reviewed spec.
2. Report `available()` honestly and without side effects. It must not download models: base models are
   referenced by `ModelRef` and must already be present as operator-provided artifacts.
3. Read only through the `ScopedArtifacts` facade it receives and write its model through that facade. It
   must not use a network path unless its descriptor declares one and policy permits it. Untrusted engines
   run through the Sandbox Fabric.
4. Be seeded-deterministic where the engine allows it. Where it cannot be, it must declare the descriptor
   determinism honestly. In either case it records the seed and config hash in the artifact.
5. Return a `TrainingRunResult` whose `semantic_authority` is `NONE`. It must not register, promote,
   authorize or deploy anything.
6. Record the engine license in `LicenseInfo`. Copyleft or external-service distribution needs operator
   license review before it is added to any contract's `allowed_backend_spdx`.
