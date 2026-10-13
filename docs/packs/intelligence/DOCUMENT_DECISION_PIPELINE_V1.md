# Deterministic Multi-Capability Intelligence Pipeline V1 (Phase G)

Packages: `pados.packs.extraction`, `pados.packs.retrieval`, `pados.packs.intelligence` (additive, never Core).
Tests: `tests/pipeline/`. Fixture generator: `tests/fixtures/pipeline/decision_document.py`.

Phase G proves Thin Core + Capability Fabric end to end with one offline, deterministic pipeline. Every stage
is a separately registered, authorized and policy-evaluated capability. The EXISTING fabric planner and
executor (`CapabilityPlanner`, `CapabilityExecutor`) run them. No new orchestrator, runner, router or
scheduler is added, no model or engine runs, and nothing uses the network.

```
installed != registered != authorized != selected != accepted
```

## Authority boundary

- These packs hold data (templates), operator wiring and adapters. They never write CAS, ScopeVersion,
  semantic state, Memory or audit, and they never import the Worker Router. A static test
  (`test_pipeline_boundaries.py`) and the integrator gate (`capability_boundary_check.py`) check this. A
  runtime test also checks that a fresh process running the whole pipeline loads none of those modules.
- No module in these packs calls an adapter, `bind_stage`, `run_bound_stage` or the OCR pack's
  `run_pipeline`. Only the fabric executor invokes adapters, through `RegisteredAdapter.guarded`. The tests
  inspect the call stack of every adapter invocation to prove this.
- Every output is a candidate (`semantic_authority = NONE`). The final explanation is labelled
  `EXPLANATION` with `authoritative: false`. The pipeline report is labelled `PIPELINE_REPORT_CANDIDATE` with
  `authoritative: false`. No receipt, artifact or report has an acceptance field. A gated task
  (`human_gate`) runs only with a CF-04 `HmacHumanDecisionVerifier` decision bound to the task and plan hash.
  That decision is recorded only as `human_decision_ref`.

## Pipeline

Task type `document_decision_analysis`, template `intelligence.document-decision-analysis` v1:

| Stage | Capability | Adapter | Determinism (capability / adapter) | Fabric verification (declared -> result) | Inputs (binding) -> outputs |
| --- | --- | --- | --- | --- | --- |
| preprocess | DOCUMENT_PREPROCESS | `ocr.preprocess.reference` | DETERMINISTIC / DETERMINISTIC | SCHEMA + DETERMINISTIC_REPLAY -> PASSED | task `document` (`ocr.source`, PDF) -> `ocr.preprocessed` |
| recognize | DOCUMENT_OCR | `ocr.pdf-text-layer` (pypdf) | PROBABILISTIC / DETERMINISTIC | SCHEMA + DETERMINISTIC_REPLAY + REFERENCE_CHECK -> PASSED (*) | task `document` + preprocess `ocr.preprocessed` -> `ocr.document` |
| layout | DOCUMENT_LAYOUT_ANALYSIS | `ocr.layout.line-grouping` | PROBABILISTIC / DETERMINISTIC | SCHEMA + DETERMINISTIC_REPLAY -> PASSED (*) | recognize `ocr.document` -> `ocr.document` |
| tables | TABLE_EXTRACTION | `ocr.table.whitespace-aligned` | PROBABILISTIC / DETERMINISTIC | SCHEMA + DETERMINISTIC_REPLAY -> PASSED (*) | layout `ocr.document` -> `ocr.document` |
| structure | DOCUMENT_STRUCTURE_PARSE | `ocr.structure.heuristic` | PROBABILISTIC / DETERMINISTIC | SCHEMA + DETERMINISTIC_REPLAY -> PASSED (*) | tables `ocr.document` -> `ocr.document` |
| extract | STRUCTURED_EXTRACTION (new) | `extraction.table-dataset` | DETERMINISTIC / DETERMINISTIC | SCHEMA + DETERMINISTIC_REPLAY -> PASSED | structure `ocr.document` -> `algorithms.dataset` |
| retrieve | PASSAGE_RETRIEVAL (new) | `retrieval.lexical-bm25` | DETERMINISTIC / DETERMINISTIC | SCHEMA + DETERMINISTIC_REPLAY -> PASSED | structure `ocr.document` -> `retrieval.ranking` |
| analyze | ROUGH_SET_RULE_LEARNING | `algorithms.drsa.domlem` | DETERMINISTIC / DETERMINISTIC | SCHEMA + DETERMINISTIC_REPLAY + REFERENCE_CHECK -> PASSED | extract `algorithms.dataset` -> `algorithms.result`, `algorithms.reproducibility` |
| explain | RESULT_EXPLANATION_TEMPLATE | `algorithms.explain.template` | DETERMINISTIC / DETERMINISTIC | SCHEMA + DETERMINISTIC_REPLAY -> PASSED | analyze `algorithms.result` -> `algorithms.explanation` |

Then the report task (task type `pipeline_report`, template `intelligence.pipeline-report` v1):

| Stage | Capability | Adapter | Determinism | Verification | Inputs -> outputs |
| --- | --- | --- | --- | --- | --- |
| report | PIPELINE_REPORT (new) | `intelligence.pipeline-report` | DETERMINISTIC / DETERMINISTIC | SCHEMA + DETERMINISTIC_REPLAY -> PASSED | task `evidence` (`intelligence.pipeline-evidence`) + `ranking` + `result` + `explanation` + `dataset` + `record` -> `intelligence.pipeline-report` |

(*) The OCR pack declares its recognition, layout, table and structure capabilities PROBABILISTIC, the
weakest determinism a conforming adapter may declare. The selected reference adapters declare DETERMINISTIC in
their digest-bound descriptors, and the fabric's DETERMINISTIC_REPLAY also keys on the SELECTED adapter's
declaration (`verification.py: VerificationEngine.verify(..., adapter_determinism=...)`, integration change
after Phase G). So these stages are replayed and PASS. Replay is a repeatability check, not a proof of
determinism: a replay that differs fails closed as `REPLAY_MISMATCH`, but a stateful, cached or falsely
declared adapter can return identical outputs twice and still PASS, and a matching replay never establishes
that an output is factually correct. A task with quality `VERIFIED` completes end to end
(`test_pipeline_e2e.py::test_verified_quality_task_completes_with_every_stage_passed`).

### Why the report is its own task

An adapter receives artifacts only, and the executor creates a stage's receipt after the stage returns. So a
report that lists every stage's capability, adapter, digests, inputs, outputs and verification cannot be a
stage of the run it describes. The process works in three steps:

1. `report.build_report_task(task, plan, result, *, store, provenance, ledger)` is trusted operator code.
   The `ExecutionResult` it is handed is a claim. It requires a `COMPLETED` run whose `PlanReceipt` passes
   `verify_structure()`, a plan that matches its own hash (`plan_hash_of`), and - CR-16 H-01 - an entry in the
   executor's trusted execution ledger under that receipt's envelope hash that records exactly this plan,
   task (id, type, hash), stage bindings and every stage row (receipt hash, verification method, status,
   verifier id, evidence hash). Anything else refuses with `REPORT_EXECUTION_NOT_IN_LEDGER`; a damaged ledger
   refuses with its own code (`EXECUTION_LEDGER_CORRUPT`, `EXECUTION_LEDGER_TORN_ENTRY`,
   `EXECUTION_LEDGER_MAC_INVALID`, ...). A fabricated chain is refused even when it is internally consistent,
   and so is a genuine run by an executor without the ledger.
   CR-16b H-04: `ledger` must be exactly a READ-ONLY `ExecutionLedgerReader` (`FileExecutionLedger.reader()`).
   The writer (`FileExecutionLedger`) belongs to the executor only; handing it to the builder, the report
   adapter or `wire_pipeline` refuses with `EXECUTION_LEDGER_READER_REQUIRED`. Anchoring is MANDATORY for
   report building: a reader without an `ExecutionAnchor` refuses with `EXECUTION_LEDGER_ANCHOR_REQUIRED`
   (and the report adapter is unavailable, so the report task is INFEASIBLE). When the operator keys the
   ledger (`mac_key_ref` + `SecretBoundary`), the reader verifies every entry's HMAC.
   It runs the algorithm pack's `verify_replay(ExpectedStage.from_receipt(...))` for the algorithm stage and
   records the verdict as ADVISORY only (CR-16b H-05): the report adapter re-performs the replay itself and
   never trusts it.
   It builds the evidence artifact: the receipt chain, the plan's input bindings, the advisory verdicts, the
   algorithm stage's replay inputs (`dataset`, `record`) and request parameters, and the final output refs.
   This artifact is `assert_minimized` with `EVIDENCE_KEYS` and holds no document text.
   Provenance (CR-16 H-02, CR-16b H-08): the outputs take the HIGHEST classification of the analysis task's
   inputs as the operator's `InputProvenance` resolves them (the caller's labels must agree; never lower).
   Every output hash and the evidence hash is resolved first: one registered for another project refuses
   (`REPORT_OUTPUT_PROJECT_CONFLICT`), one registered at a lower classification refuses
   (`REPORT_OUTPUT_CLASSIFICATION_DOWNGRADE`), and any other existing registration refuses
   (`REPORT_OUTPUT_REGISTERED_ELSEWHERE`); an identical registration is accepted (idempotent). Then all of them
   are registered in ONE `register_many` batch: insert-if-absent with conflict checking, all or nothing under
   one lock (`pados.packs.intelligence.provenance.AtomicInputProvenance`). A registration that raced in between
   refuses the whole batch (`INPUT_PROVENANCE_ENTRY_CONFLICT`) and nothing is registered. A registrar without
   `register_many` (for example the fabric's `InMemoryInputProvenance`) is refused
   (`REPORT_PROVENANCE_NOT_ATOMIC`). The evidence artifact is written only AFTER the batch succeeded; if that
   write then fails, the registration names a content hash nothing references, and a retry writes exactly
   those bytes. The report task inputs are the evidence, the final outputs and the replay inputs.
2. The fabric plans and executes the report task like any other task.
3. `PipelineReportAdapter(reader)` re-validates the evidence against the same ledger. It checks the chain
   structure, the envelope hash, the ledger entry (as above, plus `stage_bindings_hash` of the evidence's
   template and graph: `REPORT_GRAPH_NOT_RECORDED`), and the graph: every stage's inputs must be exactly the
   task inputs and upstream outputs its bindings name, in binding order, and every named task input is
   consumed. Each advisory verdict must be a PASSED `REPLAY_MATCH` by `algorithms.replay`
   (`REPORT_REPLAY_NOT_PASSED`) bound to the ledger's receipt hash (`REPORT_REPLAY_NOT_BOUND`).
   CR-16b H-05: the adapter then RE-PERFORMS the replay inside its own stage, on its authorized inputs only.
   The dataset, result and reproducibility record are inputs of the report task, read through the
   stage-scoped artifact store (hash-checked). It calls `verify_replay(ExpectedStage.from_receipt(<the
   ledger-recorded receipt>), record, result, dataset, parameters)`, where the request parameters must hash
   to that receipt's parameters hash (`REPORT_REPLAY_PARAMETERS_NOT_BOUND`, `REPORT_REPLAY_PARAMETERS_MISSING`)
   and the named replay inputs must be exactly that receipt's dataset and record
   (`REPORT_REPLAY_INPUTS_INVALID`). Only the verdict it computed is written into the report. A replay that
   does not match fails closed (`REPORT_REPLAY_FAILED`); an advisory verdict that differs from it refuses
   (`REPORT_REPLAY_ADVISORY_MISMATCH`); an input it cannot read refuses (`REPORT_INPUT_UNREADABLE`). So a
   forged PASSED verdict with a correct evidence hash, for a run whose result bytes do not replay, is refused
   (`test_pipeline_cr16b.py::test_a_forged_passed_verdict_for_a_run_that_does_not_replay_is_refused`). It also
   checks the outputs it was given, and reads each one, which re-checks the stored hash. Evidence it cannot
   resolve is refused, never labelled authentic (`REPORT_RECEIPT_STRUCTURE_INVALID`,
   `REPORT_EXECUTION_NOT_IN_LEDGER`, `REPORT_GRAPH_INCONSISTENT`, `REPORT_FINAL_OUTPUT_MISMATCH`, ...).
   V1 supports one algorithm stage per report task (`REPORT_REPLAY_BINDING_INVALID` otherwise).

The report artifact's SHA-256 is the output of the report plan's final `StageReceipt`, and that chain's
`verify_structure()` holds. The report also carries the analysis plan id and hash and the analysis receipt
envelope hash, so the two chains are linked.

## New capabilities

### STRUCTURED_EXTRACTION (`pados.packs.extraction.table_dataset`)

This capability turns one table of an `ocr.document/v1` into `algorithms.dataset/v1`. Parameters are flat
scalars with a closed schema. The task supplies them through the template's `task_parameters`:

- `table_ordinal`: tables are counted page by page.
- `object_column`: the column that holds object ids.
- `decision_column`: an optional decision column.
- `column_1` to `column_12`: each is `<header>|<name>|<TYPE>|<CRITERION>[|<domain,...>]`. TYPE is
  NUMERIC, ORDINAL or NOMINAL. CRITERION is GAIN, COST or NONE. ORDINAL columns list their domain in
  increasing order of value.

The table header must name exactly the object column plus the declared columns. Numeric cells must be
plain decimals (up to 15 integer and 9 fraction digits), and labels must belong to the declared domain.

Numeric contract (CR-16 H-03, CR-16b): a cell is accepted only when its decimal text has exactly the decimal
value of the SHORTEST ROUND-TRIP `repr` of the IEEE-754 double it parses to, and downstream algorithms
compute on that double. Otherwise it fails closed with `EXTRACTION_VALUE_NOT_EXACT`: for example
`999999999999999.999999999` would become `1000000000000000.0`, and `100000000000000.000000001` parses to the
same double as `100000000000000`. Two cells with distinct decimal values therefore never map to the same
accepted double (`0.10000000000000001`, the same double as `0.1`, is refused), so the mapping is unambiguous
and reproducible (`test_pipeline_cr16_extraction.py`). This guarantees that mapping, NOT exact binary
representation: `0.1` is accepted and is the double nearest to 0.1, and arithmetic on doubles rounds as
IEEE-754 does. The extraction itself infers, coerces and rounds nothing.

Errors are closed codes, raised outside the exception handler: `EXTRACTION_PARAMETERS_INVALID`,
`EXTRACTION_COLUMN_SPEC_INVALID`, `EXTRACTION_COLUMNS_MISSING`, `EXTRACTION_DECISION_COLUMN_UNDECLARED`,
`EXTRACTION_TABLE_NOT_FOUND`, `EXTRACTION_TABLE_HEADER_MISSING`, `EXTRACTION_HEADER_DUPLICATE`,
`EXTRACTION_COLUMN_UNDECLARED`, `EXTRACTION_COLUMN_NOT_FOUND`, `EXTRACTION_TABLE_EMPTY`,
`EXTRACTION_OBJECT_ID_INVALID`, `EXTRACTION_OBJECT_ID_DUPLICATE`, `EXTRACTION_VALUE_NOT_NUMERIC`,
`EXTRACTION_VALUE_NOT_EXACT`,
`EXTRACTION_VALUE_OUT_OF_DOMAIN`, `EXTRACTION_DATASET_INVALID` / `DATASET_*`,
`EXTRACTION_DOCUMENT_INVALID`, `EXTRACTION_INPUT_UNREADABLE`, `EXTRACTION_UNEXPECTED_ERROR`.

Evidence holds ids, counts and hashes only.

### PASSAGE_RETRIEVAL (`pados.packs.retrieval.lexical`)

This capability runs Okapi BM25 (Robertson and Zaragoza, 2009) over the reading-order passages of an OCR
document. The query and `top_k`, `k1` and `b` come from the stage parameters.

- **Tokenizer** (`pados.lexical-tokenizer.v1`): NFKC, then `casefold`, then maximal runs of letters and
  digits. The Unicode database version is recorded.
- **Scoring**: idf is `ln(1 + (N - n + 0.5) / (n + 0.5))`, summed over the distinct query terms. Scores
  are computed in a fixed `decimal` context, with no platform `libm`, and quantized to 6 decimals. Ties
  keep document order.
- **Output**: `retrieval.ranking/v1`. It holds block id, section id, page, char offsets in the canonical
  reading-order text view, score and matched-term count. It holds no document or query text, only a hash
  of the query terms, and it is `assert_minimized` before it is written.

Dense-embedding retrieval (`PASSAGE_RETRIEVAL_EMBEDDING`) and reranking (`PASSAGE_RERANK`) are separate
capabilities, so no plan can silently swap one for another. Their adapters (`retrieval.embedding-engine`,
`retrieval.reranker-engine`) are contract-only:

- They are EXPERIMENTAL with `LicenseRef-Model-Specific` and `RUNTIME_OPTIONAL`.
- They need an accelerator (CUDA, ROCM, METAL or DIRECTML; `gpu_required`).
- `available()` is always False, and `execute` raises `ENGINE_NOT_INSTALLED`.

### PIPELINE_REPORT (`pados.packs.intelligence.report`)

See above. The report is `assert_minimized` with `REPORT_KEYS` before it is written.

## Operator wiring (`pados.packs.intelligence.wiring.wire_pipeline`)

`wire_pipeline(provenance, *, operator, ledger=None, ...)` registers the capability catalog and installs and
registers the production adapters. That includes the contract-only backends and the unavailable
`RESULT_EXPLANATION` model adapter (MODEL_RUNTIME, no generator wired). `ledger` is the read-only, anchored
`ExecutionLedgerReader` of the ledger whose WRITER the operator gives to its `CapabilityExecutor(..., ledger=)`;
passing the writer refuses (`EXECUTION_LEDGER_READER_REQUIRED`). It registers both templates, plus the SCHEMA
validators and REFERENCE_CHECK hooks, and returns `PipelineWiring.invocations`, an OPTIONAL operator
cross-check (`wiring.invocations.bind(task)` around `executor.execute(plan, task)`). The provenance the
operator passes for report building must offer `register_many` (`AtomicInputProvenance`).

It returns a `PlannerConfig` with an explicit operator preference for `ocr.pdf-text-layer` and advice
disabled. It grants nothing: the project grant must name every stage capability (`template.stage_grants`).

`register_production_adapter` refuses any adapter with `production_eligible = False` or the `fixture`
backend (`PIPELINE_ADAPTER_NOT_PRODUCTION_ELIGIBLE`). The OCR pack's fixture-truth adapter is therefore
never registered.

The `ocr.document/v1` REFERENCE_CHECK (on DOCUMENT_OCR) also fails any document whose engine evidence names a
`fixture` backend. CR-16 H-02 and CR-16b H-06: the invocation comes from the EXECUTOR, never from the caller.
While it verifies a stage, `CapabilityExecutor` publishes `pados.capability.executor.current_dispatch()`: the
validated task it is executing, the plan hash, the stage id, the capability and the stage's actual bound
inputs (a minimal, backwards-compatible executor hook; `None` outside verification; adapter invocations,
including the verification replay, run in a copied context, so adapter code cannot change what later hooks
see or leave a context behind). The document's source must be
exactly that task's `document` input AND one of the stage's actual inputs, and the operator's provenance
must resolve it to that task's project and to the classification its input declares, within the task
ceiling. "Registered anywhere" is not enough, and outside an executor dispatch the check fails (never
passes). A caller binding task B around the execution of task A fails the check
(`test_pipeline_cr16b.py::test_a_caller_binding_task_b_around_task_a_is_refused`); binding nothing is fine.
Template contract (CR-16c H-09): the recognition stage must receive the task's original `document` as a DIRECT
authorized input (this template binds it next to the preprocess manifest). A template that feeds DOCUMENT_OCR only
a derived artifact fails the check closed (`VERIFICATION_FAILED`); supporting that shape needs verification of
the source through the authenticated preprocessing derivation, which V1 does not implement.

The DRSA REFERENCE_CHECK requires the reproducibility record to name the registered algorithm, adapter and
version and the template's own request parameters hash, and to describe exactly the result bytes. The full
recomputation runs at report time.

## Resource-aware planning and advice

- With a CPU-only `StaticResourceInventory` node, every stage fits (`CPU_CORES_OK`, `GPU_NOT_REQUIRED`) and
  every receipt records that resource id.
- A variant template that asks for `PASSAGE_RETRIEVAL_EMBEDDING` is INFEASIBLE on that node
  (`RESOURCE_UNAVAILABLE`, with reasons `GPU_MISSING` and `ACCELERATOR_UNAVAILABLE`). The planner never
  substitutes the lexical capability. On a CUDA node the same stage is refused with `ADAPTER_UNAVAILABLE`,
  because the engine is not installed.
- Outcome Learning advice is disabled by default. With a real `OutcomeAdvisor` enabled, the plan's feasible
  set, ordering, decision hashes and outputs are identical to the plan without advice. No pipeline
  capability has alternatives, so advice can only be recorded, never reorder anything.

## Determinism

Two full runs over fresh stores give byte-identical artifacts, an identical plan hash, identical receipt
hashes and identical envelope hashes, for the report task too. The tests inject a fixed executor clock and
run ids. The time-bearing fields are the receipts' `started_at`, `finished_at` and `duration_ms` (from the
injected executor clock), and the algorithm reproducibility record's `runtime_ms`.

The algorithm pack measures `runtime_ms` with `time.perf_counter_ns` and has no clock seam. The tests pin
that one module's counter. When it varies between execution and the fabric's replay, the analyze stage fails
closed with `VERIFICATION_FAILED` and its outputs are withheld (see the test
`test_unpinned_wall_clock_variance_fails_closed`).

The synthetic document is a pure function of its generator, and its SHA-256 is pinned in the tests.

## Known limits and integration notes (V1)

- RESOLVED at integration: the algorithm reproducibility record no longer contains `runtime_ms` (it is a
  stage metric only), so wall-clock variance never changes an artifact or a replay verdict
  (`test_pipeline_determinism.py::test_wall_clock_variance_never_changes_artifacts`). Receipts bind metrics,
  so the tests still pin the reading for byte-identical receipt envelopes (like the executor clock).
- RESOLVED at integration: the planner passes only the advisor's closed vocabulary (no features in V1) and
  learning accepts the fabric's lowercase task-type form, so `OutcomeAdvisor` computes a bound context
  (`NO_QUALIFYING_PROFILE` here, since there are no outcomes) instead of `CONTEXT_INVALID`.
- RESOLVED at integration: OCR stages are replayed by adapter determinism and PASS (see (*)).
- Data kinds: the integrator mapped the requested kinds onto the generic capability kinds instead of adding
  pipeline-specific ones: pipeline evidence is `CAPABILITY_STAGE_EVIDENCE` (R1_OPERATIONAL; content-free
  receipt chain, bindings and replay verdicts) and the report is `DERIVED_DOCUMENT` (R2_PROJECT). The extracted `algorithms.dataset` and the `retrieval.ranking` follow the OCR pack's proposed
  `OCR_INTERMEDIATE` (R0) retention.
- Tables are found by the OCR pack's whitespace-aligned detector only. Extraction requires a header row.
- CR-16 / CR-16b residuals (what one Python process cannot enforce).
  - The ledger writer and reader are separate objects and report code accepts only a reader, but inside one
    process module separation is not an isolation boundary. Same-process code that obtains the writer,
    constructs a `FileExecutionLedger` for the same path, or can resolve the MAC key reference through any
    `SecretBoundary` can still append a well-formed (and correctly MACed) entry. HMAC is symmetric, so any
    reader that verifies MACs could also compute them. This is the same class of residual as
    `ASYMMETRIC_SIGNATURE_UPGRADE = USER_DECISION_REQUIRED`; a separate append service that alone holds the key
    (or an asymmetric executor signing key) is the stronger design. The AST tests keep PADOS modules honest;
    they do not authenticate a caller.
  - The anchor is only as independent as the operator makes it: an attacker who controls the ledger file AND
    the anchor can re-seal a chain; a keyed ledger still refuses that unless the attacker can resolve the key.
    A reader holds the anchor object it was given (it only calls `latest`); an operator who wants a strictly
    read-only anchor passes an object without `record`.
  - The executor's dispatch context is a context variable. Code running in the same process can set it
    directly; the executor sets it only around verification and runs every adapter call in a copied context,
    which stops adapter code that sets it through the context variable, not hostile in-process code that
    replaces the executor module's state.
  - The report adapter's replay is a repeatability check of the recorded receipt against the CURRENT
    algorithm implementation: it proves the result bytes are what that implementation computes from the
    dataset and parameters, not that the output is factually correct.
  - Every ledger read verifies the whole file under its lock (and re-computes every MAC when keyed), so its
    cost grows with the file (bounded by `MAX_EXECUTIONS` and `MAX_LEDGER_BYTES`). Rotating the file (R1
    retention) makes older executions unresolvable, so their reports are refused.
  - Provenance registration is atomic within `AtomicInputProvenance` (one lock, one process). A registry shared
    across processes needs the same insert-if-absent batch in its own transaction.
- Data kinds and inventory: the execution ledger is `CAPABILITY_STAGE_EVIDENCE` (R1; ids, codes and hashes
  only). Requested inventory point: `capability_execution_ledger`
  (`pados.capability.execution_ledger.append_execution` via `FileExecutionLedger`, operator-chosen owner-only
  JSONL, 0700 dir, 0600 file, `Guard.ASSERT_MINIMIZED`).
