# PADOS Capability Fabric V1

Date: 2026-10-03

PADOS is a Thin Core plus a Capability Fabric. A language model is one reasoning capability among
OCR engines, parsers, deterministic algorithms, retrieval, code tools, compute resources and human
approval paths. The fabric breaks a task into the smallest capabilities and sends each stage to the
best adapter that policy allows. It never builds a monolithic LLM agent and never takes over Core
authority.

```
installed != registered != authorized != selected != accepted
```

| State | Where it happens | What it means |
| --- | --- | --- |
| installed | `CapabilityRegistry.install` | An adapter implementation object exists. Planning cannot see it. |
| registered | `register_capability` / `register_adapter` (operator) | The descriptor was re-validated and bound to a registered capability. |
| authorized | `policy.authorize_scope` from the CURRENT project grant | A project-bound grant names `CAPABILITY:<capability_id>` for this work contract. The registry never stores this. |
| selected | `CapabilityPlanner.plan` | Policy allowed the adapter for one stage, and ordering put it first. |
| accepted | outside the fabric | Only the existing ExecutionReceipt -> validation -> ResultReceipt -> CAS path accepts anything. |

## Flow

1. `TaskContract` (`task.py`) is a closed, bounded schema. It holds the task type, project, work
   contract, classification, network policy, required output contracts, quality requirement, risk level,
   human-gate flag, and named input artifact refs. Each input is project- and classification-checked.
   It also holds flat scalar parameters, which are credential-checked. It never names an adapter, an
   engine, a provider, a command or a URL.
2. `TemplateRegistry.decompose` (`graph.py`) maps the task type to an operator-registered
   `DecompositionTemplate`. Templates are data. V1 does no LLM decomposition. The result is a
   `CapabilityGraph`: a DAG of `GraphStage`s. Each stage holds a capability id, input bindings (from
   task inputs or from earlier stages' outputs, optionally filtered by artifact kind), parameters and a
   seed. Duplicate ids, unknown upstreams and cycles fail closed. The topological order is deterministic
   (Kahn's algorithm, using declaration order).
3. `CapabilityPlanner.plan` (`planner.py`) resolves every stage. The result is an `ExecutionPlan` with a
   canonical `plan_hash` and a content-derived `plan_id`.
4. `CapabilityExecutor.execute` (`executor.py`) runs a FEASIBLE plan stage by stage. It returns
   `StageReceipt`s chained from the plan genesis into a `PlanReceipt`.
5. Each stage's output is verified by `VerificationEngine` (`verification.py`). Verification is
   evidence, never acceptance.

## Registry and lifecycle

- `register_capability(descriptor, operator=...)` re-validates the descriptor, so a
  `model_construct`-forged object is refused. It also refuses duplicates.
- `install(implementation, operator=...)` re-validates the implementation's descriptor and records
  its digest.
- `register_adapter(adapter_id, capability_id=..., operator=...)`: the OPERATOR names the
  capability, and it must match the adapter's own declaration. The capability must already be
  registered. An adapter cannot weaken the capability's determinism, and only `MODEL_RUNTIME` adapters
  may declare `delegates_to_worker_router`.
- Adapters never receive the registry. The planner and executor reach implementations only through
  `RegisteredAdapter`. On every access it re-checks that the implementation's descriptor still matches
  its install digest (`ADAPTER_DESCRIPTOR_CHANGED_SINCE_INSTALL`). It also reports the lifecycle the
  operator set.
- Lifecycle transitions (`set_capability_lifecycle` / `set_adapter_lifecycle`) are explicit operator
  calls with an UPPER_SNAKE reason code. Allowed transitions: EXPERIMENTAL -> ACTIVE / DEPRECATED /
  REVOKED, ACTIVE <-> DEPRECATED, and any state -> REVOKED. REVOKED is terminal.
- Every install, registration and transition is recorded as a `RegistryEvent`. The event holds the
  sequence, subject, from/to state, operator, reason and descriptor digest. It is checked with
  `assert_minimized` (explicit `allowed_keys`) and logged. A lifecycle change changes the effective
  descriptor digest, so every decision bound to the old descriptor stops matching.

## Planner rules

For each stage, in topological order:

1. The capability must be registered. A `SEEDED` capability needs a stage seed. For quality
   `VERIFIED`, the capability must declare an automatic verification method (SCHEMA,
   DETERMINISTIC_REPLAY or REFERENCE_CHECK).
2. The stage scope comes from `authorize_scope(work_contract(CAPABILITY:<id>), current_grant)`, where
   `current_grant` is read from the trusted `GrantSource`. If the capability is not granted, the
   candidates are still explained under a scope that authorizes nothing, so each one reports
   `NOT_AUTHORIZED`.
3. The candidates are the REGISTERED adapters of the capability only. Each one is evaluated with
   `policy.evaluate`: project, grant, lifecycle, classification, network intersection, model delegation
   and availability. The availability probe runs only after every policy check passes. When a
   `ResourceInventory` is configured, the first fitting resource in `resource_id` order is recorded. If
   no resource fits, the result is `RESOURCE_UNAVAILABLE`.
4. Ordering: the operator's explicit preference list (`PlannerConfig.preferences`) comes first. The
   other candidates follow in `adapter_id` order.
5. Advice is used only when `advice_enabled` is set and a `ScoreAdvisor` is configured. The advice
   context hash comes from the advisor itself (`advisor.context_hash(...)`, which validates its own
   feature vocabulary; in V1 the task contract carries no bucketing features, so the planner passes an empty
   feature mapping and task type, capability and classification travel as explicit arguments). The planner
   binds it (SEAM-01) to the sorted feasible `(adapter_id, version)`
   pairs, the task classification value and an advice-independent `plan_binding_hash` (task hash,
   template hash, capability id, feasible set); advice bound to anything else is ignored. If that call
   raises or returns something that is not a SHA-256 hex digest, the stage gets no advice and records
   `ADVICE_CONTEXT_INVALID`. The advisor sees feasible candidates only. The planner asks `advise(...)`
   once, but it never records that return value: it reads back
   `advisor.issued(plan_binding_hash, candidates=<feasible pairs>)` (an advisor that logs identity digests
   only maps its advice back through the pairs and returns nothing for a different set),
   the advice the advisor reports it issued AND durably recorded for exactly that binding (its latest
   issuance), and records only that set (CF-01). Nothing issued gives no advice (`ADVICE_NOT_ISSUED`,
   or `ADVICE_ERROR` when `advise` raised). The issued set goes through `apply_advice`, which drops
   infeasible or foreign-context advice, refuses duplicates and clamps each value to `ADVICE_BOUND`.
   Advice reorders only the non-preferred feasible candidates: the gap between preference tiers is
   larger than 2 x `ADVICE_BOUND`. Each adjustment is recorded with its sample size, profile hash and
   explanation hash. A failing advisor changes nothing. With advice disabled, the plan is
   byte-identical to a plan built without an advisor. `OutcomeAdvisor.issued` looks up its hash-chained
   advice log by plan binding hash; its explanation is rebuilt from the logged derivation snapshot
   (`logged_explanation`), because the log holds no free text.
6. `MODEL_RUNTIME` stages are resolved through the `ModelInferenceDelegate`. The router's
   `selection_hash` and status are recorded. A missing delegate gives
   `MODEL_INFERENCE_DELEGATE_MISSING`, and a blocked router gives `WORKER_ROUTER_BLOCKED`.
7. A stage with no feasible candidate makes the plan INFEASIBLE. The plan lists every candidate's
   decision code, decision hash and failing checks. There is never a fallback to another capability,
   engine family, provider or network class.
8. At the plan level, the terminal stages' output contracts must cover `required_outputs`
   (`REQUIRED_OUTPUT_NOT_PRODUCED:<ref>`).
9. `alternatives` (other feasible adapters, in order) are recorded only when the capability declares
   `FallbackPolicy.EXPLICIT_SAME_POLICY`.

## Executor rules

Dispatch context (CR-16b H-06): while a stage's outputs are verified, the executor publishes a frozen
`DispatchContext` (task, plan hash, stage id, capability, the stage's actual bound inputs) through
`current_dispatch()`, so verification hooks read the executing task and inputs from the executor rather than
from a caller-supplied binding. Adapter invocations (the main call and any verification replay) run in a copied
context, so adapter code cannot change what the hooks read. Same-process code that replaces module state is out
of scope (in-process trust).

Before any stage runs, the executor refuses `PLAN_HASH_MISMATCH`, `PLAN_NOT_FEASIBLE`,
`PLAN_TASK_MISMATCH`, `HUMAN_DECISION_REQUIRED` (human-gated task without a reference from the
existing governed approval path) and `HUMAN_DECISION_REF_INVALID`.

Before anything runs, the executor re-derives the plan with its own `CapabilityPlanner` over the trusted
registry, templates, grants, inventory, operator preferences and the advice its advisor reports as issued
for each plan binding (`CapabilityPlanner.reproduce`; the advisor is never re-asked and the plan's own
advice is never trusted). If any stage's advice differs from the issued set — fabricated bounded advice
with recomputed hashes, an edited adjustment, profile, explanation or sample size, or any advice while no
advisor is configured — the executor refuses `ADVICE_NOT_ISSUED`. Otherwise the plan hash and id must be
identical (`PLAN_NOT_REPRODUCIBLE`); from then on only the re-derived plan runs, so a rewritten stage,
parameter, input binding, adapter or fallback list is refused even when the caller recomputed its hashes. Every input artifact is resolved through the
operator-supplied `InputProvenance` (project and classification from trusted state, never the caller's
labels); with no resolver the executor refuses to run (`INPUT_PROVENANCE_RESOLVER_MISSING`). Gated tasks
need a `HumanDecisionVerifier`-verified, single-use, HUMAN_OPERATOR decision bound to the task id and the
exact plan hash (see "Human decisions" below). The executor refuses a verifier that exposes any signing
method (`HUMAN_DECISION_VERIFIER_SIGNING_CAPABLE`, at construction and again at execution) and one that does
not consume through a durable, process-shared ledger (`HUMAN_DECISION_LEDGER_NOT_DURABLE`).

Then, for each stage, immediately before execution (TOCTOU):

- It reads the effective capability and adapter descriptors from the registry.
- It re-derives the stage scope from the CURRENT grant (`AUTHORIZATION_REVOKED_BEFORE_EXECUTION`).
- It re-checks the planned resource against the inventory (`RESOURCE_NO_LONGER_FITS`).
- It re-evaluates policy (`POLICY_<code>`). The decision hash must equal the planned one
  (`POLICY_DECISION_CHANGED_SINCE_PLANNING`), and the capability digest must be unchanged.
- Model inference: it makes a fresh delegate selection. The selection must be ROUTED and its hash
  must equal the planned hash (`WORKER_ROUTER_SELECTION_CHANGED`). The executor keeps that evidence and
  resolves `router_selection_hash` to it before execution.
- `permitted_inputs` = the validated task's input refs plus outputs written by earlier stages of this
  run. Caller-supplied hashes are never accepted.
- `bind_stage` + `run_bound_stage`. Adapters are invoked only through `RegisteredAdapter.guarded`:
  `available()` and the descriptor integrity check (adapter code) run first, then the executor's final
  recheck (no adapter code): recorded descriptors and digests, lifecycle, current grant, resource fit,
  `evaluate()` decision hash, router evidence and input provenance. A revocation triggered from inside
  `available()` is therefore still caught. Fallback alternatives and verification replay use the same
  guard; a refused replay withholds the outputs.
- Post-execution revalidation (CF-03): immediately after the adapter call returns, still under the
  dispatch lock and before any output is accepted or verified, the executor runs the same authoritative
  recheck again: grant (and its hash), lifecycle, recorded capability/adapter descriptors and digests, the
  implementation's descriptor integrity, resource fit, the `evaluate()` decision hash, input provenance
  and Worker Router evidence. If anything changed while the adapter ran, ALL outputs of that call are
  withheld with `REVOKED_DURING_EXECUTION`: the receipt records the refusal and references no output,
  nothing is verified, nothing reaches `ExecutionResult.outputs`, and downstream stages are
  `NOT_EXECUTED_UPSTREAM_FAILED`. Fallback alternatives and verification replays use the same guarded path,
  so the same holds for them (a refused replay withholds the first run's outputs as well).
- Residual (Codex CR-13d, after the third fixer cycle; recorded as `CROSS_AUTHORITY_DISPATCH_TRANSACTION =
  ARCHITECTURAL_RESIDUAL`): the grant source, inventory, provenance and router are separate, partly frozen
  authorities that expose no shared version or lease. The fabric re-reads them one after another, so a
  revocation landing between two re-reads, or between the pre-check and the invocation, is not a single
  atomic cut, and nothing undoes side effects of a call already running. Closing this needs coordinated
  authority versions, leases or a dispatch transaction across those authorities, which would reopen frozen
  authority interfaces and is out of scope for the Capability Fabric. Outputs are still never accepted when
  a change is visible at the post-check, and every later dispatch re-checks.
- Linearization point (CF-03): that post-execution check is where the fabric accepts a call's outputs as
  candidates. A revocation from ANY authority — registry, grant source, resource inventory,
  `InputProvenance` or the inference delegate — that completes before the check reads that authority
  prevents the outputs' use. One that completes after it is ordered after acceptance; it still refuses every
  later dispatch, replay and downstream stage, which run their own checks. Registry changes from another
  thread cannot land inside the window at all: every registry mutator (install, register, lifecycle) takes
  the registry's dispatch lock (a re-entrant `threading.RLock`; no thread is started), which the guarded
  call holds from the final pre-call check until the post-execution check has passed, so they complete
  either before the pre-call check (refused, e.g. `LIFECYCLE_CHANGED_BEFORE_EXECUTION`) or after the
  post-execution check. The other authorities are not locked by the fabric; each seam is re-read inside the
  lock and must return a consistent snapshot of its own state (operator responsibility). A running
  in-process call cannot be preempted.
- Same-thread mutation (CF-03): reentrancy alone would let adapter code (`available()`, `descriptor`,
  `execute`) or a seam callback invoked by the checks mutate the registry from the dispatching thread. A
  per-thread dispatch-in-progress flag (`threading.local`, set from the readiness probe until the
  post-execution check ends) refuses every registry mutator on that thread with
  `REGISTRY_MUTATION_DURING_DISPATCH`. The guarded call is then refused with the same code even if the
  callback swallowed the error, so no output survives an attempted mutation. Registry calls from the same
  thread work again as soon as the dispatch is over.

Failure semantics:

- Adapter errors fail closed: no output refs. Downstream stages get `NOT_EXECUTED_UPSTREAM_FAILED`
  receipts, so the chain is still complete.
- Partial outputs are kept (`partial=True`, `PARTIAL_OUTPUT`) only for `PARTIAL_WITH_EVIDENCE`
  capabilities. Otherwise the result is `PARTIAL_OUTPUT_NOT_PERMITTED` with no outputs.
- Verification `FAILED` withholds outputs (`VERIFICATION_FAILED`). Under quality `VERIFIED`, anything
  short of `PASSED` fails the stage.
- Fallback applies only to adapter errors (`CapabilityAdapterError`). The executor walks the planned
  `alternatives` in order. Each one is fully re-prepared (policy, decision hash, resource) and must pass
  `fallback_permitted`. The `FallbackTransition.transition_hash` is recorded in the receipt.
- An adapter that reads an unauthorized artifact fails with `ADAPTER_ARTIFACT_ACCESS_DENIED`. That
  failure is never eligible for fallback.

## Human decisions (CR-13b, CR-13c)

Issuance and verification are separate capabilities.

- `human_decision.py` only verifies. `HmacHumanDecisionVerifier(operator_keys=..., boundary=...,
  ledger=...)` maps every registered operator to that operator's OWN SecretRef key (a key shared by two
  operators is refused). A record is checked only under the key of the operator it names, so a caller who
  names an operator without holding that operator's key never produces a verifiable decision. The verifier
  holds only key REFERENCES and the boundary; the key is resolved inside one narrow function for one
  comparison at verification time, and nothing is retained afterwards (no key bytes, resolved value, MAC or
  cache; a test counts one resolution per verification and compares the verifier's slots before and after).
  It has no `issue`, sign or MAC-producing method.
- `operator_decisions.py` is the operator-side issuer, and `operator_cli.py` (`main()`, run as
  `python -m pados.capability.operator_cli`) is its only importer under `src/pados`. AST tests show that
  no execution-side fabric module (executor, planner, registry, verification, inference, adapter, graph,
  task, provenance, human_decision, decision_ledger, seams, policy, receipts, artifacts, model) and no pack
  imports either module or names their entry points.
- `open_operator_issuer(approver=..., operators=..., boundary=..., environ=None)` needs an
  operator mapping that binds EVERY approver to `OperatorBinding(key_ref, OperatorAccount(...))`: that
  approver's own SecretRef and OS account. Each key and each account belongs to one approver only
  (`HUMAN_DECISION_OPERATOR_KEY_SHARED`, `OPERATOR_ACCOUNT_SHARED`); a POSIX binding without a uid is
  refused (`OPERATOR_ACCOUNT_BINDING_INCOMPLETE`). The current process must BE the bound account, read from
  the OS and never from a caller-supplied value: on POSIX the real uid (equal to the effective uid) and its
  `pwd` login name. Otherwise `OPERATOR_SESSION_MISMATCH`. On Windows issuance FAILS CLOSED
  (`OPERATOR_PLATFORM_UNSUPPORTED`, CR13d-01): the environment-derived account name is spoofable, and a
  token/SID-based identity plus file-ACL checks are `WINDOWS_NATIVE_VALIDATION = PENDING`.
- The session must be interactive: stdin and stdout are TTYs. No caller can supply the session (CR13d):
  `open_operator_issuer` and `operator_cli.main` have no session parameter; the terminal is
  `TerminalSession` through the private `_session_factory` seam that only tests replace, and the typed
  confirmation is read from the controlling terminal (`/dev/tty`), never from a redirected stdin. Otherwise
  `OPERATOR_SESSION_NOT_INTERACTIVE`.
- `approve(task_id=..., plan_hash=..., issued_at=..., lifetime=...)` is the explicit approval action. It
  re-checks automation markers, the OS account and the interactive session, then asks through the
  session's TTY reader for the first 12 hex characters of the plan hash and for the task id (the prompts
  never display them). Anything but an exact match refuses with `OPERATOR_CONFIRMATION_MISMATCH`, and
  nothing is signed. It always signs in the issuer's own approver name.
- CI and automation markers (`CI`, `GITHUB_ACTIONS`, ...) and automation-looking approver names are refused
  at opening and again at approval.
- `operator_cli` reads the mapping from an owner-only JSON file (`pados.capability.operator-bindings/v1`;
  a group/other-accessible or linked file is refused as `OPERATOR_BINDINGS_UNSAFE` on POSIX), resolves the
  key from the operator's environment through `SecretBoundary`, and writes the signed record to `--out`
  (created exclusively, 0600, `assert_minimized` with the record schema). It never prints the record. Every
  refusal prints its closed code to stderr and exits 1.

What this authenticates: the OS login session (the process runs as the account the operator mapping binds to
that approver) plus an explicit, interactive human confirmation of that exact plan and task at a TTY, outside
CI/automation. What it does NOT authenticate: an attacker who controls the same OS account and its TTY (they
ARE that session), or in-process code that can resolve the operator's SecretRef (through this or any other
`SecretBoundary`) and compute the HMAC itself, or replace private module state. Inside one Python process,
module separation and the private session seam are not an isolation boundary; the operator mapping is trusted configuration, so whoever can
write it can bind an approver to their own account.
- The caller's record is copied ONCE into a plain `dict` of plain `str` (non-plain mappings, subclasses or
  non-string values are refused as `HUMAN_DECISION_MALFORMED`). Validation, authentication, consumption
  and the returned reference use only that snapshot (CR13b-03).
- Single use is enforced by `decision_ledger.FileConsumedDecisionLedger`: an atomic consume-if-unused under
  the process-safe `pados.lifecycle.locking` store lock, owner-only (0700 directory, 0600 file, no links,
  no foreign owner), hash-chained (`seq`, `prev_hash`, `entry_hash`) and minimized (`schema, seq,
  decision_id, consumed_at, prev_hash, entry_hash`, checked by `assert_minimized`). A restart, a second
  verifier or a second process sees every earlier consumption. A corrupt chain fails closed
  (`HUMAN_DECISION_LEDGER_CORRUPT`); a full ledger refuses (`HUMAN_DECISION_LEDGER_FULL`).
- Complete appends (CR13c-02): an entry is appended by repeating `os.write` until every byte is written
  (a zero-byte or impossible count refuses), and `fsync` runs only after the complete entry. Any failure
  refuses the consumption with `HUMAN_DECISION_LEDGER_UNAVAILABLE`, so execution never starts. The decision
  is then consumed (the full entry reached the file) or refused: with zero progress nothing was written and
  it authorized nothing; with partial progress the file ends in a torn line, and every later open — any
  verifier, any process — refuses with `HUMAN_DECISION_LEDGER_TORN_ENTRY` until an operator repairs the
  file. The torn tail is never truncated automatically, since that would silently make the refused decision
  usable again. The in-memory
  ledger exists only as `in_memory_ledger_for_tests()`; the executor refuses it and no non-test module
  calls it (static test).
- Provisional DataKind: `CAPABILITY_DECISION_LEDGER` (R3 authority metadata). It is not yet in the
  lifecycle catalog; the integrator adds it there.

Residual: `ASYMMETRIC_SIGNATURE_UPGRADE = USER_DECISION_REQUIRED` for the symmetric-secret part. HMAC is
symmetric, so in-process code that can resolve an operator's key through a boundary can compute a valid MAC
without the operator's session or confirmation. Only an asymmetric, operator-held signing key removes that;
it needs a new crypto dependency and alignment with the frozen governance primitives, which use the same
operator-held HMAC model.

## Execution ledger (CR-16 H-01, CR-16b H-04 / H-07)

Receipts are evidence, and evidence handed around by a caller is a claim. `execution_ledger.py` is the trusted,
append-only record of what the executor actually ran.

- Writer and reader are separate objects (CR-16b H-04). `FileExecutionLedger` is the WRITER: the operator
  constructs it for the executor, which holds it (`CapabilityExecutor(..., ledger=FileExecutionLedger(path,
  anchor=...))`; optional and backwards compatible). With it, every execution that produced a `PlanReceipt`
  (COMPLETED or FAILED) is appended through `append_execution` before the result is returned. If the append
  fails, the executor refuses with the ledger's closed code, so no result leaves the executor unrecorded.
  Without `ledger=` nothing changes. AST tests show that `executor.py` is the only module under `src/pados`
  that names the writer class or `append_execution`.
- `ExecutionLedgerReader` (`FileExecutionLedger.reader()`, or constructed from the same path and anchor) is
  READ ONLY: `resolve`, `entries`, `verify`. It has no append path and holds no reference to a writer (only
  the path, the capacity, the anchor's `latest` and the optional MAC key reference; slots only). Consumers
  call `require_reader(...)`, which accepts exactly this type: a writer-capable object refuses with
  `EXECUTION_LEDGER_READER_REQUIRED`, and an unanchored reader with `EXECUTION_LEDGER_ANCHOR_REQUIRED`.
  Anchoring is therefore mandatory wherever a report is built from the ledger. `append_execution` refuses a
  reader (`EXECUTION_LEDGER_INVALID`), and so does the executor.
- An entry is content-free: the envelope hash, plan id and hash, task id, type and hash, a
  `stage_bindings_hash` (template id and hash plus every stage's capability and input bindings), the status,
  and per stage, in order, `[stage_id, receipt_hash, verification_method, verification_status, verifier_id,
  evidence_hash]`. It passes `assert_minimized` with `ENTRY_KEYS` (plus `entry_mac` when keyed) before it is
  written and when it is read.
- The file follows the discipline of the consumed-decision ledger. It is owner-only (0700 directory, 0600
  file, no links, no foreign owner) and hash-chained (`seq`, `prev_hash`, `entry_hash`). It runs under the
  `pados.lifecycle.locking` store lock. Appends are complete (`os.write` is repeated until every byte is
  written, and `fsync` runs only after the full entry). Reads fail closed: a broken chain gives
  `EXECUTION_LEDGER_CORRUPT`, and a torn tail gives `EXECUTION_LEDGER_TORN_ENTRY`; a torn tail is never
  truncated automatically.
- Bounded before writing (CR-16b H-07): under the lock, an append is refused with `EXECUTION_LEDGER_FULL`
  BEFORE anything is written when the verified file size plus the serialized entry would exceed
  `MAX_LEDGER_BYTES`, or when the ledger already holds `capacity` entries. A successful append therefore
  never leaves a file that later reads refuse.
- Anchoring: an `ExecutionAnchor` kept outside the writable store holds the latest `(seq, entry_hash)` by
  compare-and-set (the pattern of the learning advice-log anchor; no code is shared). Every read requires the
  file head to equal the anchored head (`EXECUTION_LEDGER_ANCHOR_MISMATCH`). This catches a whole-file rewrite
  with a recomputed chain.
- Optional entry authentication: with `mac_key_ref` (a `secretref://` reference) and a `SecretBoundary`, the
  writer adds `entry_mac`, an HMAC-SHA256 over the entry hash under the executor key, and a reader configured
  with the same reference verifies every entry (`EXECUTION_LEDGER_MAC_INVALID`; an entry without a MAC is
  refused too). An unresolvable or short key refuses (`EXECUTION_LEDGER_KEY_UNAVAILABLE`). The key is resolved
  inside one function for one scan or append and is never stored, returned or persisted. This stops a party
  that can rewrite the ledger file AND its anchor but cannot resolve the key.
- `LedgerEntry.matches(receipt)` is true only for exactly the recorded receipt chain. A consumer such as the
  pipeline report builder refuses any other chain, including a fabricated chain that passes
  `verify_structure()`.
- Residual (what one Python process cannot enforce; `ASYMMETRIC_SIGNATURE_UPGRADE = USER_DECISION_REQUIRED`
  class): module separation is not an isolation boundary. Same-process code that obtains the writer,
  constructs a `FileExecutionLedger` for the same path, or can resolve the MAC key reference through any
  `SecretBoundary` can still append a well-formed, correctly MACed entry; HMAC is symmetric, so every reader
  that verifies MACs could also compute them. The AST tests keep PADOS modules honest; they do not
  authenticate a caller. A separate append service that alone holds the key (or an asymmetric executor
  signing key) is the stronger design.
- Replay evidence recorded here is repeatability evidence. DETERMINISTIC_REPLAY by the selected adapter's
  declaration checks that a second run gives the same output hashes, and a replay that differs fails closed
  (`REPLAY_MISMATCH`). It cannot prove determinism: a stateful, cached or falsely declared adapter can return
  identical outputs twice. A matching replay never establishes that an output is factually correct.
- DataKind: `CAPABILITY_STAGE_EVIDENCE` (R1). Requested inventory point: `capability_execution_ledger`.
  Rotating the file makes older executions unresolvable, so they are refused, never accepted.

## Verification

| Method | Fabric behavior |
| --- | --- |
| SCHEMA | The operator-registered validator for the capability's `output_contract` checks the output bytes. A missing validator gives NOT_VERIFIED. A crashing validator gives FAILED. |
| DETERMINISTIC_REPLAY | The stage is re-run through `run_bound_stage` (revalidated) and the output hashes must match. This only applies to DETERMINISTIC capabilities, or SEEDED capabilities with a seed. |
| REFERENCE_CHECK | An operator-registered hook compares the outputs with a trusted reference or fixture. |
| HUMAN_REVIEW / UNIT_TESTS | Performed by existing governed paths. They never give PASSED here. |
| NONE | NOT_VERIFIED. |

The receipt records `verification_method`, status, `verifier_id` and `verification_evidence_hash`
(the canonical hash of the per-method results and output hashes). `VerificationResult` has no
acceptance field.

## Delegation to the Worker Router

`inference.py` is the only fabric module that imports `pados.worker.router.select_worker`. A static
test enforces this. `WorkerRouterBridge` works as follows:

- An operator mapping turns the fabric capability id into an existing router `CapabilityId`.
- It narrows the router's `allowed_worker_types` to worker types whose existing provider profile
  (`pados.enterprise.compute.profile_for_worker` and `resolve_requirement`) permits the task
  classification and network policy. `select_worker` supports this narrowing natively, and it never
  changes the router's ranking.
- It calls `select_worker` read-only. It returns an `InferenceSelection` whose `selection_hash` is
  the canonical hash of the router decision (status, selected worker, error, explanation, considered
  workers), bound to the request (project, stage, capability, risk, classification, network, grant
  hash).

The planner never imports provider code and never ranks language-model providers.

## Provenance

Every `StageReceipt` binds the following: plan id, stage id and attempt id
(`<run>.<stage>.<attempt>`); capability id, family, version and digest; adapter id, version and
digest; resource id; determinism; input and output artifact refs (SHA-256); parameters hash; seed;
timings; verification method, status, verifier and evidence hash; policy decision hash; fallback
transition hash; Worker Router selection hash; partial flag; failure code; and bounded metrics.
Receipts are linked with `receipts.chain(plan_id, plan_hash, ...)`. `PlanReceipt.expected_stage_ids`
equals the plan's stages, so `verify_structure()` proves the chain is complete and bound to this plan.
It proves only that. It is not proof of acceptance. `human_decision_ref` is set only from a verified
human decision (`human-decision:<id>`) and is a reference, never acceptance. The fabric never sets human
acceptance, and `semantic_authority` is always `NONE`.

## Boundaries (enforced by `tests/capability/test_fabric_boundaries.py`)

- CORE_REMAINS_THIN: no module in `product_loop`, `state_proof`, `memory`, `worker`, `remote`,
  `execution`, `compute_node`, `connectivity` or `enterprise` imports `pados.capability` or
  `pados.packs`.
- Fabric V1 modules do not import state-proof service/repository/audit, product-loop state/service,
  memory, db, the Core scheduler/router/task service, or semantic-context/handoff writers.
- Only `inference.py` touches `select_worker`. The planner, executor, registry, graph, task and
  verification modules import no provider code.
- No subprocesses, sockets, threads, schedulers or HTTP clients in fabric V1 modules. The registry imports
  `threading` only for its dispatch `RLock` and dispatch flag (static test).
- The operator issuer (`operator_decisions.py`) is imported only by its entry module (`operator_cli.py`);
  neither is imported by an execution-side module or a pack, and nothing imports the entry module. The
  test-only in-memory decision ledger is never called by a non-test module.
- The registry uses `threading` only for its dispatch `RLock` and the per-thread dispatch flag
  (`threading.local`); it starts no thread.
- Descriptor `operation` and `description` are catalog text. The planner, executor, registry, graph
  and verification modules never read them.

## Known limits (V1)

- `StageRequest` carries only `router_selection_hash`. A delegating model adapter must resolve the
  selected worker from the bridge evidence that the operator wires into it. The contract has no
  `selected_worker_id` field.
- Fallback triggers only on adapter errors, not on verification failures.
- The consumed human-decision ledger is durable and is never pruned automatically (R3). Rotating its file
  is safe only after the 24 h decision lifetime has passed since the last issuance, or together with
  rotating the operator keys; otherwise an unexpired decision could be consumed again.
- Registry mutators on other threads wait for in-flight guarded adapter calls, and guarded calls on one
  registry are serialized. A hung in-process adapter therefore also delays registry changes; containment of
  adapter code is out of scope for V1. Registry mutators on the dispatching thread itself are refused
  (`REGISTRY_MUTATION_DURING_DISPATCH`).
- An adapter call that ran while an authority changed is not undone: its side effects (artifacts written to
  the content-addressed store, any external effect of in-process code) remain; only the use of its outputs is
  refused (`REVOKED_DURING_EXECUTION`).
- A revocation that completes after the post-execution check (for example while a SCHEMA validator runs)
  does not withdraw the outputs that check accepted; it applies to every later dispatch, replay and
  downstream stage. Stages verified by DETERMINISTIC_REPLAY get a second post-execution check at the replay.
- Advice is bound to the advisor's LATEST issuance for a plan binding: re-planning the same task replaces
  it, and an older plan carrying the earlier advice is refused (`ADVICE_NOT_ISSUED`); re-plan instead.
- An adapter whose `available()` flips between planning and execution makes the plan irreproducible
  (fails closed).
- Artifacts written by a stage that later fails stay in the content-addressed store, unreferenced by
  any receipt. The fabric never deletes from the store.
- The plan hash proves integrity, not authority. Execution accepts only a plan the executor can
  reproduce from trusted state, and it still has to pass every execution-time policy re-check.
