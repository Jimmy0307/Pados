# Algorithm Capability Pack V1

Package: `pados.packs.algorithms` (additive, never Core). Tests: `tests/packs/algorithms/`.

Principle: an LLM must not pretend to be a numerical or structured algorithm when a real algorithm exists.
This pack provides deterministic, pure-Python reference implementations as Capability Fabric adapters.
It adds no runtime dependencies, needs no network, starts no threads or timers, and holds no authority.

## Authority boundary

- Adapters run only through `pados.capability.adapter.bind_stage` / `run_bound_stage`. They see only a
  `ScopedArtifacts` facade: they can read only the stage's authorized input and write new artifacts.
- Every output is a candidate (`semantic_authority = NONE`). Acceptance happens only through the existing
  receipt -> validation -> CAS path. This pack never imports state-proof write paths, product-loop state,
  Memory, CAS or audit modules, or the Worker Router (static test: `test_algo_boundaries.py`).
- Being installed is not the same as being authorized: each algorithm is its own capability, so a project grant has to
  name `CAPABILITY:<capability_id>`.
- No implicit fallback: every capability uses `FallbackPolicy.NONE`, so nothing silently switches from an algorithm to an LLM.

## Capabilities

| Capability id | Algorithm id | Status | Adapter |
| --- | --- | --- | --- |
| `ROUGH_SET_RULE_LEARNING` | `drsa.domlem` v3 | ACTIVE | `algorithms.drsa.domlem` |
| `FUZZY_COGNITIVE_MAP` | `fcm.inference` v3 | ACTIVE | `algorithms.fcm.inference` |
| `MULTI_CRITERIA_DECISION_DEMATEL` | `mcdm.dematel` v2 | ACTIVE | `algorithms.mcdm.dematel` |
| `MULTI_CRITERIA_DECISION_DANP` | `mcdm.danp` v0 | EXPERIMENTAL, contract only | `available() == False` |
| `MULTI_CRITERIA_DECISION_CHOQUET` | `mcdm.choquet` v0 | EXPERIMENTAL, contract only | `available() == False` |
| `MULTI_CRITERIA_DECISION_ROV` | `mcdm.rov` v0 | EXPERIMENTAL, contract only | `available() == False` |
| `RESULT_EXPLANATION` | (model) | MODEL_RUNTIME, delegates to the Worker Router | `algorithms.explain.model` |
| `RESULT_EXPLANATION_TEMPLATE` | (template) | deterministic stand-in | `algorithms.explain.template` |

All algorithm capabilities have these properties: family `ALGORITHM`, `Determinism.DETERMINISTIC`, `LOCAL_ONLY`, ceiling
`RESTRICTED`, verification `SCHEMA` + `DETERMINISTIC_REPLAY` + `REFERENCE_CHECK`, and license AGPL-3.0-only
(`Distribution.NONE`, owned by PADOS).

MCDM is a generic contract that every method shares. All methods use the same dataset input and result envelope, and
`result.method` names the method. Each method is a separate capability (`MULTI_CRITERIA_DECISION_<METHOD>`), so a
grant authorizes one method's semantics and a fallback can never replace DEMATEL with a different method.

## Contracts (`contracts.py`)

- Dataset (`pados.algorithms.dataset.v1`, artifact kind `algorithms.dataset`, `application/json`). It holds columns with
  `type` (NUMERIC / ORDINAL with the domain in increasing order / NOMINAL with a closed domain), `criterion`
  (GAIN / COST / NONE, used for dominance), optional numeric `minimum`/`maximum`, an optional `decision` column, and
  `missing` (REJECT, the default, or DROP_OBJECT, where the dropped count is reported). It also holds `object_ids` and `rows`.
  Size limits: 64 columns, 5000 rows, 100k cells and 8 MiB. Parsing is strict: no duplicate keys, no NaN or
  Infinity, depth of 16 or less. Credential-shaped content is refused.
- Numbers are bounded before any conversion. A JSON number literal longer than 32 characters is refused
  (`JSON_NUMBER_TOO_LARGE`) before `int()`/`float()` runs, a literal that overflows to infinity (`1e999`) is
  `JSON_NON_FINITE_NUMBER`, and a dataset number or column bound with |x| > 10^15 is
  `DATASET_NUMBER_OUT_OF_RANGE` / `DATASET_BOUNDS_INVALID` (an integer is range-checked before it becomes a
  float, so a huge integer can never raise `OverflowError`). `-0` and `-0.0` are normalized to `0.0`.
- Dataset hash: SHA-256 of the canonical serialization, so different encodings of the same dataset produce the same hash.
- Result (`pados.algorithms.result.v1`, kind `algorithms.result`). It holds the algorithm id and version, capability id,
  dataset hash, normalized-parameters hash, seed, normalized parameters, the method result (validated against the
  method's result schema), and deterministic metrics. It holds no timing or host data, so the artifact SHA-256 is the
  result hash.
- Reproducibility record (`pados.algorithms.reproducibility.v1`, kind `algorithms.reproducibility`). It holds the
  algorithm id and version, adapter id, parameters hash (normalized), request parameters hash (the same value as
  `StageReceipt.parameters_hash`), dataset hash, dataset artifact SHA-256, seed, result hash, metrics, the compute target, `deadline_seconds` and `completion`. `completion` is `DETERMINISTIC` when no wall-clock
  deadline applied, and `NONDETERMINISTIC_DEADLINE` when one did: then WHETHER the run completes depends on the
  host, while a completed result is still deterministic (see "Wall-clock deadline"). The two fields must agree
  (`RECORD_COMPLETION_INCONSISTENT`). Before it is written, the record passes `assert_minimized` with the
  explicit `RECORD_KEYS` schema.
- `load_result(bytes, ref)` is the consumer-side loader. It refuses any artifact that is not a canonical result, and
  that includes explanations.
- Fail closed: everything is computed and validated before the first write, and every error is a closed
  `UPPER_SNAKE` code that does not echo content (for example `DATASET_MISSING_VALUE`, `ALGORITHM_PARAMETERS_INVALID`,
  `DEMATEL_MATRIX_SINGULAR`).
- Closed-error boundaries. The public loaders (`parse_dataset`, `load_result`, `load_record`,
  `AlgorithmSpec.parse_parameters`) raise their `AlgorithmError` outside the handler of the underlying
  exception, so neither `__cause__` nor `__context__` holds JSON decoder state, pydantic input values or
  minimization paths. The adapters (`AlgorithmAdapter`, both explanation adapters) map an `AlgorithmError` to
  `CapabilityAdapterError(code)` and any other exception to `ALGORITHM_UNEXPECTED_ERROR` /
  `EXPLANATION_UNEXPECTED_ERROR` / `EXPLANATION_GENERATOR_FAILED`, again with an empty exception chain. Only the
  fabric's own `ArtifactAccessDenied` (a closed code from the stage facade) passes through unchanged.
  `verify_replay` never raises: an unexpected exception is the verdict `REPLAY_UNEXPECTED_ERROR`.

## Algorithms (sources: public method definitions only)

### DRSA + DOMLEM (`drsa.py`)

- Greco, Matarazzo and Słowiński, *Rough sets theory for multicriteria decision analysis*, EJOR 129 (2001). This source
  defines the dominance relation, the cones D+(x) and D-(x), the upward and downward unions, their lower and upper
  approximations and boundaries, the accuracy, and the quality of approximation.
- Greco, Matarazzo, Słowiński and Stefanowski, *An algorithm for induction of decision rules consistent with the
  dominance principle*, RSCTC 2000. This is the source for DOMLEM. Elementary conditions come from positive objects
  that are not yet covered. The best condition has the highest consistency `|[E]∩B|/|[E]|`, with ties broken by
  coverage of uncovered objects. Redundant conditions and redundant rules are removed, and only minimal rules are kept
  across unions, starting with the strongest union.
- Implementation choices:
  - Regular attributes (criterion NONE) take part in dominance by equality.
  - COST criteria use preference `-value`, so their displayed operators are flipped.
  - Ties are broken deterministically: the lower criterion position wins, then the more general threshold.
  - Candidates that do not change `[E]` are skipped. Some candidate always narrows `[E]` for a true lower
    approximation, so this guarantees termination.
- Limits: 1000 objects, 32 criteria, 32 classes, and `max_rules` 5000 or less (default 500).
- Operation budget (v2, extended in v3). `operation_budget` (default 20,000,000, maximum 100,000,000) bounds the
  work deterministically. It is counted in elementary steps, not in wall-clock time, from the first step on:
  - table construction (v3): one step per cell read by the missing-value policy, per criterion cell converted
    (an ordinal cell costs its domain size), and per decision-class lookup (domain size per row);
  - threshold masks (v3): one step per object read and per distinct value when a mask is first built;
  - union membership (v3): one step per object for each union;
  - one step per dominance-cone conjunct, approximation membership test, threshold collected,
    candidate-condition evaluation, coverage-check conjunct and rule-redundancy union;
  - generalization (v3): one step per candidate plus one per CONDITION compared (v2 charged one step per
    accepted rule, whatever the rules' length);
  - output (v3): one step per object for each reported object list and per reported rule value lookup.

  `OperationBudget.spend` refuses before the limit would be passed, so `used <= operation_budget` always holds.
  The run stops with `DRSA_OPERATION_BUDGET_EXCEEDED` at the first step past the budget, anywhere from table
  construction to the reported output. The count is reported as the `operations` metric. The students example
  spends 128 steps on its table before the first cone.
- `max_rules` is enforced inside induction (v2). It bounds the rules held at any moment: the accepted rules plus
  the working rules of the union being covered. Another rule is refused (`DRSA_RULE_LIMIT_EXCEEDED`) as soon as it
  would push that number past `max_rules`, before its conditions are searched. This also bounds the output. It is
  stricter than v1, which checked only accepted rules after a union had been fully induced: a run whose working set
  exceeds `max_rules` before redundancy elimination now aborts. The students example needs `max_rules >= 6`.
- Measured on the review VM (structureless random data, 1000 objects × 31 criteria; an ad-hoc probe, not a
  test): with the default budget every case stops within about 8.5 s (continuous values) or 2.5 s (integer values);
  at the maximum budget the slowest case (32 classes, continuous) stops with `DRSA_OPERATION_BUDGET_EXCEEDED` after
  about 41 s. With 2 classes and `max_rules = 5000` the run completes (470 rules, 4 s; about 10.1 M operations
  under v2 and 12.1 M under v3's fuller metering). Polling the default deadline added about 6 % to the
  default-budget case (6.7 s → 7.1 s). Wall-clock figures depend on the host; the operation count does not.
- Reference test: the students example (Słowiński, Greco and Matarazzo tutorial, *Search Methodologies*, 2005). The
  approximations, quality (0.75) and the full rule set are derived by hand in `test_algo_drsa.py`.

### Fuzzy Cognitive Map (`fcm.py`)

- Kosko (1986); modified Kosko (Stylios and Groumpos, 2004); rescaled rule (Papageorgiou, 2011).
- Transfer functions: sigmoid, tanh (steepness λ), bivalent and trivalent.
- The weight matrix is a square dataset: row i, column j holds w_ij in [-1, 1]. Self-loops are refused.
- Inference stops with one of three statuses: `FIXED_POINT` (max |ΔA| ≤ ε), `LIMIT_CYCLE` (within ε of one of the
  last `max_period` states), or `MAX_ITERATIONS`. `MAX_ITERATIONS` is reported, not hidden.
- Clamped concepts (v3) keep their EXACT initial value, the decimal the user wrote, for the whole computation,
  so their influence on other concepts uses that value. Only their reported representation (final state,
  trajectory) is rounded, half-even on the decimal, to `precision` places like every other reported value.
  v2 quantized a clamped value before iterating: with precision 1 a concept clamped at 0.14 acted as 0.1
  (`test_algo_fcm_clamped.py`: with λ = 1.7 its neighbour now reaches 0.6 instead of 0.5). A clamped value is
  constant, so it never affects the epsilon test or cycle detection.
- Concepts: at most 64. Iterations: at most 500.
- Replay determinism (v2). The platform `libm` is not used. Each step is computed with the standard-library
  `decimal` module in a fixed context (40 significant digits, ROUND_HALF_EVEN), whose `exp` and division are
  correctly rounded by specification. Floats enter as the decimal their shortest repr denotes. The new state (and
  the initial state) is rounded half-even to `precision` decimals BEFORE the epsilon test and cycle detection, so
  the trajectory, the status and the iteration count are decided on quantized values. v1 compared unrounded
  `exp`/`tanh` results and rounded only the reported values, so its control flow could depend on last-ulp `libm`
  differences. That claim has been withdrawn. Free (unclamped) concepts are quantized; clamped ones are exact
  (see above).
- Expected values: none of the hand-computed tests changed under quantization. They all use precision 10, and
  their quantized trajectories equal the rounded float ones. A coarse precision changes the trajectory on purpose.
  For example, precision 2 with epsilon 1e-12 reaches a fixed point after 4 steps instead of 14
  (`test_algo_fcm_quantized.py`).

### DEMATEL (`mcdm.py`)

- Gabus and Fontela (1972/1973). Normalization and threshold follow Tzeng, Chiang and Li (2007).
- Normalize: s = max(max row sum, max column sum), then N = A/s.
- Compute T = N(I − N)^-1 with exact rational Gauss–Jordan elimination (`fractions.Fraction`, no floats). A singular
  `I − N` raises `DEMATEL_MATRIX_SINGULAR` explicitly. There is no pseudo-inverse and no approximation.
- Derived scores: D, R, prominence D+R, and relation D−R (CAUSE, EFFECT or NEUTRAL). Edges are entries t_ij above the
  threshold (default: the mean of T).
- Limits: 32 factors at most, non-negative entries, zero diagonal.
- Bounded exact arithmetic. Every entry, and an explicit `threshold_value`, must be a decimal with at most 9 places
  (`DECIMAL_PLACES`). There is no silent rounding:
  - a subnormal float is refused with `DEMATEL_VALUE_SUBNORMAL`;
  - a value with more places, or an extreme exponent such as `1e-300`, is refused with
    `DEMATEL_VALUE_PRECISION_EXCEEDED` (`DEMATEL_THRESHOLD_INVALID` for the threshold).
- Per-step bit check (v2). Every single rational operation is checked against `MAX_FRACTION_BITS = 8192` right
  after it runs: each product, quotient and difference of Gauss–Jordan elimination, and each product and
  PARTIAL SUM of the matrix product, the normalisation sums and the score sums. v1 checked only after a whole
  row or dot product had been formed, so an oversized intermediate that cancelled again was never seen.
  Exceeding the cap raises `DEMATEL_ARITHMETIC_BUDGET_EXCEEDED`. For admissible input (32 factors, integer
  multiples of 10^-9 up to 10^6) the Hadamard bound is below about 2,000 bits. The measured worst case needs
  1,731 bits, so the cap is a backstop that valid input does not reach.
- Arithmetic-operation budget (v2). One `ArithmeticBudget` is shared by normalisation, the inverse, the product and
  the scores; every rational operation is one step. Parameter `operation_budget`: default 1,000,000, maximum
  10,000,000. The budget refuses before the limit would be passed, with `DEMATEL_OPERATION_BUDGET_EXCEEDED`. The
  count is reported as the `operations` metric. A 3-factor matrix needs 235 steps (counted by hand in
  `test_algo_dematel_steps.py`). The 32-factor worst case needs about 204,000 steps (1.3 s on the review VM).
- Contract-only methods (parameter and result schemas only):
  - DANP (DEMATEL-based ANP).
  - Choquet integral (a capacity over subsets of criteria).
  - ROV, read as *range of value* (Yakowitz, Lane and Szidarovszky, 1993).

## Verification: DETERMINISTIC_REPLAY (`replay.py`)

`verify_replay(expected, record_bytes, result_bytes, dataset_bytes, parameters)` recomputes the run with the
current implementation. It trusts neither the record nor the result. The trusted caller supplies `expected`, an
`ExpectedStage`, and replay checks every field of it. Build it with `ExpectedStage.from_binding(binding, output)` from
the executor's own `StageBinding` and observed `StageOutput`, or with `ExpectedStage.from_receipt(receipt)`. It holds:

- capability id and digest;
- adapter id, version and digest;
- request parameters hash;
- the trusted request seed: `from_binding` takes `binding.request.seed`, `from_receipt` takes the receipt's
  `seed` (whose semantics the receipt validators bind). The algorithms are DETERMINISTIC and execution refuses
  every seed, so replay requires it to be `None`. A seed the caller did not establish (the `SEED_UNKNOWN`
  default of a hand-built `ExpectedStage`) fails closed;
- input and output artifact references.

Each mismatch has its own closed code:

| Check | Code |
| --- | --- |
| algorithm unknown / version differs | `REPLAY_ALGORITHM_UNKNOWN` / `REPLAY_ALGORITHM_VERSION_MISMATCH` |
| record capability / adapter differs from the registered one | `REPLAY_RECORD_CAPABILITY_MISMATCH` / `REPLAY_RECORD_ADAPTER_MISMATCH` |
| record carries a seed (execution refuses every seed) | `REPLAY_SEED_NOT_APPLICABLE` |
| record request hash ≠ `parameters_hash(parameters)` (the shared helper the adapter uses) | `REPLAY_RECORD_REQUEST_PARAMETERS_HASH_MISMATCH` |
| result bytes / dataset bytes ≠ the record | `REPLAY_RESULT_HASH_MISMATCH` / `REPLAY_DATASET_ARTIFACT_MISMATCH` |
| binding capability id / digest | `REPLAY_BINDING_CAPABILITY_MISMATCH` / `REPLAY_BINDING_CAPABILITY_DIGEST_MISMATCH` |
| binding adapter id / version / digest | `REPLAY_BINDING_ADAPTER_MISMATCH` / `..._ADAPTER_VERSION_MISMATCH` / `..._ADAPTER_DIGEST_MISMATCH` |
| binding request parameters hash | `REPLAY_BINDING_PARAMETERS_HASH_MISMATCH` |
| binding seed not established / not `None` | `REPLAY_BINDING_SEED_UNKNOWN` / `REPLAY_BINDING_SEED_NOT_APPLICABLE` |
| binding input (exactly the dataset artifact) / outputs (exactly result, record) | `REPLAY_BINDING_INPUT_MISMATCH` / `REPLAY_BINDING_OUTPUT_MISMATCH` |
| canonical dataset hash / normalized parameters hash | `REPLAY_DATASET_HASH_MISMATCH` / `REPLAY_PARAMETERS_HASH_MISMATCH` |
| recorded metrics ≠ recomputed metrics | `REPLAY_METRICS_MISMATCH` |
| recomputed result hash ≠ recorded | `REPLAY_DIVERGED` |
| replay deadline invalid / passed | `REPLAY_ALGORITHM_DEADLINE_INVALID` / `REPLAY_ALGORITHM_DEADLINE_EXCEEDED` |
| any other exception | `REPLAY_UNEXPECTED_ERROR` |

The recomputed envelope always has `seed = None`; it never copies the record's seed. A record's
`compute_target`, `deadline_seconds` and `completion` cannot be checked by recomputation, so a match does not
vouch for them. They are telemetry only. `verify_replay(..., deadline_seconds=None)` optionally bounds the
recomputation by wall-clock time (see below).

## Wall-clock deadline (`meter.py`)

- `AlgorithmAdapter(spec, deadline_seconds=...)`. Default `DEFAULT_DEADLINE_SECONDS` = 300 s, at most
  `MAX_DEADLINE_SECONDS` = 3600 s, `None` disables it; anything else is refused with `ALGORITHM_DEADLINE_INVALID`.
  It is adapter configuration, not an algorithm parameter, so it is not part of any parameters hash.
- The deadline starts when `execute` starts. The work meters (DRSA `OperationBudget`, DEMATEL `ArithmeticBudget`)
  poll `time.monotonic` on every step, and FCM polls it once per iteration. No thread or timer is involved.
- It only ABORTS: `ALGORITHM_DEADLINE_EXCEEDED`, with no output written. It never changes a value, so a completed
  result is identical with or without a deadline (`test_algo_work_meter.py` compares the bytes).
- Whether a run completes before the deadline depends on the host. The record states this:
  `completion = NONDETERMINISTIC_DEADLINE` together with `deadline_seconds`. A run that hit the deadline can
  be retried with a longer deadline or none; the deterministic operation budgets still bound it.

The returned `evidence_hash`, `verifier_id = algorithms.replay` and `method` fill the verification fields of a
`StageReceipt`. This is verification evidence, not acceptance.

## Explanations (`explanation.py`)

- `RESULT_EXPLANATION` (MODEL_RUNTIME, PROBABILISTIC, verification HUMAN_REVIEW). The adapter has
  `delegates_to_worker_router = True`, so `run_bound_stage` refuses the stage without Worker Router selection evidence.
  The adapter has these limits:
  - It can consume only one validated `algorithms.result` artifact. It takes no parameters and no dataset.
  - The source must name a registered, executable algorithm at its current version and capability. Otherwise it is
    refused with `EXPLANATION_ALGORITHM_UNKNOWN`, `..._VERSION_MISMATCH` or `EXPLANATION_CAPABILITY_MISMATCH`.
  - The `result` body must validate against that algorithm's own result schema
    (`EXPLANATION_RESULT_SCHEMA_INVALID`). Facts are read only from the validated model. `algorithm_version` and
    `capability_id` are pattern-bounded in every result and record.
  - The whole facts payload is bounded before any generator runs: at most 16 KiB of canonical JSON, 64 items per
    collection and 2048 characters per string (`EXPLANATION_FACTS_TOO_LARGE`). It also passes `assert_minimized`
    with the explicit `FACT_KEYS` schema (`EXPLANATION_FACTS_CREDENTIAL_SHAPED`, `EXPLANATION_FACTS_NOT_MINIMIZED`).
    The deterministic template explainer applies the same checks.
  - The model receives only bounded facts derived from the result, as a private deep copy.
  - The text is bounded, has no control characters and is credential-free. It is written as a separate
    `algorithms.explanation` artifact labelled `EXPLANATION`, with `authoritative: false` and the source result hash.
  - Without a wired `ExplanationGenerator`, the adapter is unavailable.
- `RESULT_EXPLANATION_TEMPLATE` is a deterministic, model-free stand-in.
- The tests check each of these properties:
  - a hostile generator that returns a forged result and mutates its facts cannot change the result bytes, the
    source hash, or replay;
  - `load_result` refuses explanations, including a relabelled reference;
  - explanations cannot take explanations or datasets as input.

## Integration notes (outside this pack's allowlist)

- No new `DataKind` was needed. Records are stored only as content-addressed artifacts through the stage's
  `ArtifactStore`. If an executor persists reproducibility records elsewhere, it should register a DataKind for them
  (R1_OPERATIONAL is suggested), and records must still pass `assert_minimized(..., allowed_keys=RECORD_KEYS)`.
- The fabric executor should wire an `ExplanationGenerator` to the worker that `select_worker` chose (through the
  `ModelInferenceDelegate` bridge) and pass that selection hash as `StageRequest.router_selection_hash`.
