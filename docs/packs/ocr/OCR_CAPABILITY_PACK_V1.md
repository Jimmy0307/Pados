# OCR / Document Perception Capability Pack V1

Status: implemented as an additive pack (`src/pados/packs/ocr/`). No OCR engine is bundled, installed or
downloaded. `OCR_LIVE_ENGINE_UAT = PENDING`: no third-party engine has been exercised live yet.

## Authority boundary

- The pack is not Core. Core never imports `pados.packs` (a static test checks this).
- Capabilities are descriptive metadata. Adapters are replaceable code. Outputs are candidates
  (`semantic_authority = NONE`) that reference content-addressed artifacts. Acceptance goes only through the
  existing receipt -> validation -> CAS path.
- installed != registered != authorized != selected != accepted. Each stage needs a project grant naming
  `CAPABILITY:<capability_id>`, a policy decision of `ALLOWED` from `pados.capability.policy.evaluate`,
  and execution through `bind_stage` / `run_bound_stage`. That path re-validates the grant immediately
  before the adapter runs.
- Nothing falls back implicitly. A text layer never silently becomes OCR, and OCR never silently becomes a
  model. Recognizing scanned pages after the text layer is a separate stage that the plan names explicitly
  (`ocr.pdf-hybrid`).
- Model inference (vision-language OCR) is a `MODEL_RUNTIME` capability. It delegates to the existing
  Worker Router through `ModelInferenceDelegate`. Its receipt carries the router's selection hash. The pack
  never ranks providers.

## Contracts

| Contract | Meaning |
| --- | --- |
| `ocr.request/v1` (`OcrTaskContract`) | The input `ArtifactRef` (kind `ocr.source`), the media type (`application/pdf`, `image/png`, `image/jpeg`, `image/tiff`), an optional page range (at most 2000 pages, never clipped), up to 8 BCP-47-subset language hints, `preserve_layout`, `require_tables`, the classification, the network policy (default `LOCAL_ONLY`) and the quality requirement (`min_mean_confidence`, `allow_needs_ocr_pages`). |
| `ocr.preprocessed/v1` (`PreprocessManifest`) | Validated facts about the input: the detected media type, page count, selected pages and page geometry. It holds no content. |
| `ocr.document/v1` (`OcrDocument`) | Pages, each with a status (`TEXT_LAYER`, `RECOGNIZED`, `NEEDS_OCR`, `FAILED`), blocks (`LINE`, `PARAGRAPH`, `HEADING`: text, normalized bbox or null, confidence or null), tables (rectangular cells), figure references and reading order. Also sections, plus evidence: input hash, engine records (adapter, version, backend, engine version, SPDX, distribution, operation, attempted pages, perceived pages), stage marks, a per-page confidence summary, and the `NEEDS_OCR` and `FAILED` pages. The evidence holds no content and passes `assert_minimized`. |
| `ocr.engine-result/v1` (`EngineResult`) | The one neutral shape every engine binding returns: pages, then lines, each with text, an absolute bbox and a confidence from 0 to 1. Results are normalized and bounded. A page outside the selection, a bbox outside the page, or an unknown field fails closed. |

Coordinates are normalized to the page (0..1, origin top-left). A field is `null` when the producer did not
measure it. Embedded PDF text has no recognition confidence, and pypdf's public API exposes no glyph geometry.

## Capabilities (each a separate, replaceable stage)

| Capability | Family | Input -> output | Determinism (weakest allowed) | Ceiling / network |
| --- | --- | --- | --- | --- |
| `DOCUMENT_PREPROCESS` | PERCEPTION | request -> preprocessed | DETERMINISTIC | RESTRICTED / LOCAL_ONLY |
| `DOCUMENT_OCR` | PERCEPTION | preprocessed -> document | PROBABILISTIC | RESTRICTED / LOCAL_ONLY (partial output allowed, with evidence) |
| `DOCUMENT_LAYOUT_ANALYSIS` | DOCUMENT_INTELLIGENCE | document -> document | PROBABILISTIC | RESTRICTED / LOCAL_ONLY |
| `TABLE_EXTRACTION` | DOCUMENT_INTELLIGENCE | document -> document | PROBABILISTIC | RESTRICTED / LOCAL_ONLY |
| `FIGURE_EXTRACTION` | DOCUMENT_INTELLIGENCE | document -> document | PROBABILISTIC | RESTRICTED / LOCAL_ONLY |
| `DOCUMENT_STRUCTURE_PARSE` | DOCUMENT_INTELLIGENCE | document -> document | PROBABILISTIC | RESTRICTED / LOCAL_ONLY |
| `VISION_MODEL_OCR` | MODEL_RUNTIME | preprocessed -> document | PROBABILISTIC | CONFIDENTIAL / LOCAL_ONLY (EXPERIMENTAL) |
| `VISION_MODEL_OCR_CLOUD` | MODEL_RUNTIME | preprocessed -> document | PROBABILISTIC | INTERNAL / WAN_REQUIRED (EXPERIMENTAL) |

A capability's determinism is the weakest determinism its adapters may declare. Tests check this for every
adapter, and the runner refuses an adapter that does not conform. The cloud vision capability is separate
from the local one, so it needs its own grant. The shared policy refuses it above INTERNAL data and under
a `LOCAL_ONLY` or `PRIVATE_NETWORK` task. A WAN adapter can never implement the local-only `DOCUMENT_OCR`
capability (`ADAPTER_WIDENS_CAPABILITY_NETWORK`).

## Engine matrix

| Adapter | Capability | Backend | License (SPDX) | Engine version | Distribution | Status |
| --- | --- | --- | --- | --- | --- | --- |
| `ocr.pdf-text-layer` | DOCUMENT_OCR | pypdf | BSD-3-Clause | installed pypdf (6.14.2 in `requirements.lock`) | RUNTIME_DEPENDENCY (declared PADOS dependency) | REAL, local, deterministic; live-tested on synthetic PDFs |
| `ocr.fixture-truth` | DOCUMENT_OCR | fixture | AGPL-3.0-only | n/a | NONE | TEST/CONTRACT ONLY, EXPERIMENTAL, `production_eligible = False`; admitted only with the `TEST_FIXTURE_MODE` token; output marked `FIXTURE` |
| `ocr.tesseract-cli` | DOCUMENT_OCR | tesseract-cli | Apache-2.0 | declared by the operator | RUNTIME_OPTIONAL | CONTRACT ONLY; unavailable until configured; OCR_LIVE_ENGINE_UAT = PENDING |
| `ocr.paddleocr` | DOCUMENT_OCR | paddleocr | Apache-2.0 | declared by the operator | RUNTIME_OPTIONAL | CONTRACT ONLY (operator `EngineBinding` = `module:function`, run only in the bounded binding worker); OCR_LIVE_ENGINE_UAT = PENDING |
| `ocr.mineru-style` | DOCUMENT_OCR | mineru | AGPL-3.0-only | declared by the operator | RUNTIME_OPTIONAL | CONTRACT ONLY (`EngineBinding`, bounded binding worker), EXPERIMENTAL; LICENSE REVIEW REQUIRED (network copyleft; model weights have model-specific licenses); OCR_LIVE_ENGINE_UAT = PENDING |
| `ocr.vision-model.local` | VISION_MODEL_OCR | worker-router | LicenseRef-Model-Specific | per model | RUNTIME_OPTIONAL | CONTRACT ONLY, EXPERIMENTAL, delegates to the Worker Router; OCR_LIVE_ENGINE_UAT = PENDING |
| `ocr.vision-model.cloud` | VISION_MODEL_OCR_CLOUD | worker-router | LicenseRef-Provider-Terms | per provider | EXTERNAL_SERVICE | CONTRACT ONLY, EXPERIMENTAL, WAN, data leaves the host; OCR_LIVE_ENGINE_UAT = PENDING |
| `ocr.preprocess.reference` | DOCUMENT_PREPROCESS | pados-reference | AGPL-3.0-only | n/a | NONE | REAL reference stage |
| `ocr.layout.line-grouping` | DOCUMENT_LAYOUT_ANALYSIS | pados-reference | AGPL-3.0-only | n/a | NONE | REAL reference stage |
| `ocr.table.whitespace-aligned` | TABLE_EXTRACTION | pados-reference | AGPL-3.0-only | n/a | NONE | REAL reference stage |
| `ocr.figure.pdf-xobject` | FIGURE_EXTRACTION | pypdf | AGPL-3.0-only | n/a | NONE | REAL reference stage (lists figures; does not extract their bytes). The license is the PADOS-owned adapter's; pypdf itself is BSD-3-Clause |
| `ocr.structure.heuristic` | DOCUMENT_STRUCTURE_PARSE | pados-reference | AGPL-3.0-only | n/a | NONE | REAL reference stage |

Engine adapter notes:

- **Tesseract.** The adapter accepts images only. A PDF needs rasterization first, and V1 has no engine for
  that. The argv is fixed: `<exe> stdin stdout -l <codes> --psm 3 [--tessdata-dir <dir>] tsv`. It never
  uses a shell. The image goes in on stdin. The environment is minimal (`OMP_THREAD_LIMIT=1`, `LC_ALL=C`),
  so no credentials are inherited and replay is single-threaded. It runs through the pack's bounded runner
  (see "Process isolation and budgets"). The configured binary must pass `executable_refusal`: absolute,
  no symlinked path component, regular, executable, not setuid/setgid, no file capabilities. Otherwise
  the adapter is unavailable, and a direct run fails with `ENGINE_EXECUTABLE_INVALID`. It is started by
  the `_rlimit_exec` launcher, which re-validates it, applies the limits and then execs it: wall clock and
  CPU = `timeout_seconds` (1-600 s), memory = `memory_mb` (default 2048), stdout capped at
  `max_output_bytes` (default 8 MiB) and stderr at `max_stderr_bytes` (default 64 KiB), both drained
  incrementally. Any overrun kills the original process group (`ENGINE_TIMEOUT`,
  `ENGINE_OUTPUT_TOO_LARGE`, `ENGINE_BUDGET_EXCEEDED`). The engine ALWAYS gets `RLIMIT_NPROC = 0`: it
  can create no process and no thread, which a single-threaded build under `OMP_THREAD_LIMIT=1` does
  not need. A build that does need one fails closed (`ENGINE_FAILED`), and there is no switch to relax
  it (`deny_process_creation` no longer exists). The tessdata path must also be absolute operator
  configuration. Language hints map through a bounded BCP-47 table, and an unmapped hint fails with
  `LANGUAGE_UNSUPPORTED`. Image decoding (PNG, JPEG) happens inside the engine, which runs inside the
  bounded child. The pack validates the header budget (dimensions and pixel count) before recognition.
  Tests use a fake executable written to a temporary directory; real Tesseract is never run.
- **PaddleOCR and MinerU.** Their Python APIs change between releases, so the pack does not hard-code one.
  The operator configures an `EngineBinding`, a `module:function` reference with its own budget, which
  is NEVER imported or called in the PADOS process. It runs in the bounded binding worker (see "Engine
  bindings") and returns `ocr.engine-result/v1`. `available()` also needs the engine package and the
  binding's package to be findable (nothing is executed to check). A binding failure becomes
  `ENGINE_FAILED` (or one of the closed `BINDING_CODES`), raised with no `__cause__` and no
  `__context__`, and no binding text leaves the child.
- **Vision models.** The operator supplies a `VisionModelBinding`, which must call the worker the Worker
  Router selected. The adapter refuses a request that has no router selection hash. The model runtime's
  containment is router-side.

## Pipeline

Templates are plain data (`DecompositionTemplate.as_data()`, `template_hash`). They name capabilities and
never adapters, so the Fabric planner can register them:

| Template | Stages |
| --- | --- |
| `ocr.pdf-text-layer` | preprocess -> recognize -> layout -> tables* -> figures -> structure |
| `ocr.pdf-hybrid` | preprocess -> recognize -> recognize_scanned (fills only `NEEDS_OCR` pages) -> layout -> tables* -> figures -> structure |
| `ocr.image` | preprocess -> recognize -> layout -> tables* -> structure |
| `ocr.image-vision-local` / `ocr.image-vision-cloud` | the same, with `VISION_MODEL_OCR` / `VISION_MODEL_OCR_CLOUD` for recognize |

`*` The tables stage runs only when the task sets `require_tables`. A template without a table stage
refuses such a task.

`run_pipeline` is the pack's small local runner, used by tests and integration. For each stage it takes the
adapter the caller assigned. It never ranks, selects or substitutes. It evaluates the shared policy, refuses
EXPERIMENTAL adapters unless `allow_experimental=True`, refuses fixture adapters unless the test-mode token
is passed (below), and gets the Worker Router selection for model-runtime stages. It binds and runs each
stage through `run_bound_stage`. Every stage gets a `StageReceipt` (policy decision hash, parameters hash,
verifier evidence), and the receipts are chained into a `PlanReceipt`. Any refusal (`OcrPipelineRefused`)
or failure (`OcrPipelineFailed`) stops the run with a closed code, raised with no exception chain.

Each output is checked against its schema and the task's source, and then verified independently of what
its producer claims:

| Check | Closed code |
| --- | --- |
| The preprocess stage is the pack's own isolated IMPLEMENTATION: the adapter's type is exactly `DocumentPreprocessAdapter` (a subclass is refused even with the registered descriptor), its descriptor digest is in `REGISTERED_PREPROCESS_DIGESTS`, the instance shadows no method of the class in its `__dict__` (`execute`, `_execute`, ...), and its budget is a `ProcessBudget`. This is checked on the assigned adapter before anything runs, never taken from the manifest | `PREPROCESS_ADAPTER_NOT_REGISTERED` (refusal) |
| The preprocess manifest selects exactly the task's page range (or every page) | `STAGE_OUTPUT_PAGES_MISMATCH` |
| Every document covers exactly the manifest's selected pages, in order | `STAGE_OUTPUT_PAGES_MISMATCH` |
| `pages_total`, `needs_ocr_pages`, `failed_pages` and every per-page confidence summary are recomputed from the pages (and, for pages whose lines a later stage dropped, from the previous verified document) | `STAGE_OUTPUT_SCHEMA_INVALID` (model-level recomputation) or `STAGE_OUTPUT_EVIDENCE_MISMATCH` |
| The stage marks extend the previous document's by exactly this stage and adapter | `STAGE_OUTPUT_PROVENANCE_INVALID` |
| A recognition stage appends exactly one engine record, and that record must EQUAL the one the verifier rebuilds from the assigned descriptor (adapter id and version, backend, engine version, license SPDX and distribution, the capability's operation) and the page policy below. Perceived pages name it as producer; pages it did not attempt are unchanged | `STAGE_OUTPUT_PROVENANCE_INVALID` / `STAGE_OUTPUT_PAGE_STATUS_CHANGED` |
| Any other stage keeps the engine records and every page's status, producer and geometry | `STAGE_OUTPUT_PROVENANCE_INVALID` / `STAGE_OUTPUT_PAGE_STATUS_CHANGED` |
| Outside test mode, no document may carry a fixture mark or a `fixture` engine; inside it, the mark is never dropped | `STAGE_OUTPUT_FIXTURE_PROVENANCE` / `STAGE_OUTPUT_PROVENANCE_INVALID` |

Engine-record page policy (explicit): the FIRST recognition stage attempts every selected page
(`attempted_pages` = the manifest's selected pages) and its perceived pages (`pages`) are those it made
`TEXT_LAYER` or `RECOGNIZED`. A LATER recognition stage attempts exactly the previous document's
`NEEDS_OCR` pages, may only turn them `RECOGNIZED` (or `FAILED`), and its perceived pages are those it
made `RECOGNIZED`. A page that is perceived always carries blocks.

A `RECOGNIZED` page must have at least one block. An engine page with no non-blank line is not a
recognition result: V1 has no explicit blank-page result, so such a page stays `NEEDS_OCR`. The quality
gate (`QualityVerdict`) recomputes page statuses from the pages and reports `REQUESTED_PAGES_MISSING`,
`NEEDS_OCR_PAGES_PRESENT`, `FAILED_PAGES_PRESENT`, `CONFIDENCE_BELOW_REQUIREMENT` and
`CONFIDENCE_UNMEASURED`; `met` means every requested page was perceived. With a confidence floor, EVERY
`RECOGNIZED` page needs a verified measurement, whatever its blocks look like by the end.

The measurement policy is `MEASUREMENT_POLICY = "LINE_UNWEIGHTED_MEAN_MIN/v1"` (`page_measurement`). It is
computed ONLY from the page's validated LINE-block confidences, and every LINE block must carry one.
There must be at least one. `mean` is their unweighted arithmetic mean (each line counts once, whatever
its length or box), and `minimum` is their minimum. PARAGRAPH and HEADING blocks, tables and figures
never contribute, so extra high-confidence non-LINE blocks cannot lift it. The function computes it
directly from the LINE blocks and does not reuse any other summary. The runner takes it from each
recognition stage's own verified output, carries it past every later stage, and exposes it as
`PipelineResult.measurements`. A later stage that drops or merges lines can therefore neither erase
nor invent it. A page whose recognition was paragraph-only or only partly scored is
`CONFIDENCE_UNMEASURED`. Called directly without measurements, `evaluate_quality` recomputes them from
the document's current LINE blocks, never from its claimed evidence. The gate reports these but does
not accept anything.

Page coverage trusts the pack's own isolated preprocess implementation: the manifest's page count comes
from the worker's pypdf page tree (or the validated image header). That is why the runner refuses any
other preprocess adapter, including a subclass or an instance with overridden methods, instead of
trusting what a manifest claims. The descriptor digest is metadata, not proof of implementation. (Code
in the PADOS process that patches the class itself is outside this boundary.) An integrator executor that runs these
stages outside `run_pipeline` must apply the same rule (`pipeline.is_registered_preprocess`).

### Test mode (fixture adapters)

`FixtureOcrAdapter` (and any adapter with `production_eligible = False` or a `fixture` backend) is refused
with `FIXTURE_ADAPTER_REQUIRES_TEST_MODE` unless the caller passes
`run_pipeline(..., test_fixtures=TEST_FIXTURE_MODE)`. `allow_experimental=True` alone never admits it, and
anything other than a `FixtureMode` token is refused (`TEST_MODE_TOKEN_INVALID`). Production code paths
never pass the token. A test-mode run is marked everywhere: `PipelineResult.provenance = "FIXTURE"`
(otherwise `"ENGINE_CANDIDATE"`), every `StageReceipt` carries the metric `test_fixture_mode = 1` (inside
the hash chain), and every document a fixture adapter produced carries `evidence.test_provenance =
"FIXTURE"`, which later stages inherit. Production documents omit the field, so their bytes are unchanged.

## Lineage

`register_ocr_lineage(graph, source_id=..., result=..., trusted_sources=...)` adds nodes under the registered
`SOURCE`. `trusted_sources` maps source node ids to the SHA-256 of their source artifact, taken from the
caller's own registry. Registration is refused unless the entry for `source_id` equals the hash of the
document's source (`LINEAGE_SOURCE_MISMATCH`, `LINEAGE_SOURCE_IDENTITY_UNKNOWN`). Every stage output becomes
a `NORMALIZED_ARTIFACT`, and each section of the final document becomes a `CHUNK` under the final document.
`LineageGraph.plan_revocation(source)` therefore reaches every OCR derivative, and `apply_revocation` masks
them. Node ids are deterministic. The returned record holds only ids and hashes and passes
`assert_minimized`. Registration is all-or-nothing: every node and edge is computed and checked (kind
conflicts, revoked nodes) before the graph is touched, and if applying them fails the graph is restored
from a snapshot of its public fields (`LINEAGE_REGISTRATION_ROLLED_BACK`).

## Process isolation and budgets

pypdf never parses an untrusted PDF in the PADOS process, and an operator engine binding never runs
in it. Preprocess (page count, encryption, MediaBox), the figure XObject scan and text-layer extraction
run in `pados.packs.ocr._pdf_worker`; PaddleOCR / MinerU-style bindings run in
`pados.packs.ocr._binding_worker`. `pados.packs.ocr._isolated` launches both as
`sys.executable -I -B -m <worker> <limits> [<module:function>]` with `shell=False`, a minimal
environment, a request header and the input bytes on stdin, and one bounded canonical JSON reply on
stdout. The same runner (`run_bounded`) runs Tesseract through the `_rlimit_exec` launcher
(`sys.executable -I -B -m pados.packs.ocr._rlimit_exec <limits> -- <binary> ...`).

How a child is started and limited (POSIX):

1. **Nothing is spawned by a privileged process.** `isolation_refusal()` (the probe is
   `_rlimit_exec.privilege_refusal`; its `_identity` and `_proc_status` are the test seams) refuses when
   the real or effective uid is 0 (`PRIVILEGED_UID`, `PRIVILEGED_EUID`) or, on Linux, when
   `/proc/self/status` shows `CAP_SYS_RESOURCE` (bit 24) or `CAP_SYS_ADMIN` (bit 21) in `CapEff`,
   `CapPrm` or `CapAmb` (`CAP_SYS_RESOURCE`, `CAP_SYS_ADMIN`), or cannot be read
   (`CAPABILITIES_UNREADABLE`). The kernel does not enforce `RLIMIT_NPROC` for such a process. The
   runner then raises `OCR_ISOLATION_UNAVAILABLE` without spawning, and every adapter that needs it
   reports `available() == False` with `unavailable_reason() == "OCR_ISOLATION_UNAVAILABLE"`.
2. **Nothing is exec'd that fails validation** (`_rlimit_exec.executable_refusal`). The operator's binary
   must be an absolute, normalized path in which no component is a symlink. It must be a regular,
   executable file that is not setuid/setgid and has no `security.capability` file capabilities.
   It is opened with `O_NOFOLLOW` (`O_PATH` on Linux) and must be the same inode that was checked.
   Tesseract's `available()` applies this check. The runner applies it again before spawning
   (`ENGINE_EXECUTABLE_INVALID`), and the launcher applies it a third time in the child, right before
   its `execv` (exit 119). The interpreter the children start as is PADOS's own `sys.executable`. A
   virtual environment's `bin/python` is a symlink by design and must remain the exec target, because
   it selects the environment, so the validation runs on its resolved file.
3. The wall-clock deadline is taken BEFORE the process is spawned, so startup counts against it.
   The child is started by a plain `subprocess.Popen` (`_posixsubprocess` fork+exec in C) in its own
   session, with no `preexec_fn`. No Python code runs between fork and exec, so a multithreaded caller
   cannot deadlock the child. If `Popen` returns after the deadline has already expired, the child is
   SIGKILLed at once, before any input is written (`OCR_PARSE_TIMEOUT` / `ENGINE_TIMEOUT`).
4. The child applies the limits to itself as its first action. The PDF and binding workers do so in
   `main`, before they read stdin or import pypdf or the operator module. For a binary, the launcher
   sets them and then `os.execv`s the validated absolute path, and the limits survive exec. The limits
   travel as one fixed-format argv token
   (`pados-rlimits/2:as=..,cpu=..,fsize=0,nofile=..,core=0,nproc=0`). There is no other `nproc` value.
   The child first re-checks that it is unprivileged. It then sets each limit and reads it back. On
   Linux it finally sets `PR_SET_NO_NEW_PRIVS`, which is irreversible and survives exec, and reads that
   back too, so a setuid/setgid program or file capabilities cannot raise its privileges later. If any
   of these steps fails, the child exits 117 without doing anything else, and the call fails with
   `OCR_ISOLATION_UNAVAILABLE`.
5. After the final group kill, reaping waits at most `REAP_SECONDS` (5 s). A child stuck in
   uninterruptible kernel sleep is left for `subprocess` to reap later, and the call still returns
   (`TIMEOUT`).

| Budget (PDF default) | Value | Code when exceeded |
| --- | --- | --- |
| Source size, checked before the source is read or any child starts | 64 MiB | `OCR_SOURCE_TOO_LARGE` |
| Wall clock, from before the spawn (startup included) | 60 s | `OCR_PARSE_TIMEOUT` |
| `RLIMIT_CPU` (soft; hard = soft + 1) | 30 s | `OCR_PARSE_BUDGET_EXCEEDED` |
| `RLIMIT_AS` memory | 1 GiB | `OCR_PARSE_BUDGET_EXCEEDED` |
| `RLIMIT_NPROC` = 0 (no process, no thread), mandatory for every child | fixed | the fork / thread start fails |
| pypdf decompression limits, set from the budget before parsing (see below) | 64 MiB per stream | `OCR_DECODE_BUDGET_EXCEEDED` |
| Decoded content streams per page, decoded BEFORE `extract_text` (`max_page_content_bytes` = `max_page_text_bytes` x `CONTENT_DECODE_FACTOR` 8) | 64 MiB | `OCR_DECODE_BUDGET_EXCEEDED` |
| Decoded PAGE content streams (`/Contents` only, not Form XObjects), running total over the pages (`max_content_bytes` = `max_text_bytes` x 8) | 384 MiB | `OCR_DECODE_BUDGET_EXCEEDED` |
| Entries of one page's `/Contents` array, counted before any is resolved | 10000 | `OCR_DECODE_BUDGET_EXCEEDED` |
| Extracted text per page, measured right after each page's extraction | 8 MiB | `OCR_PARSE_BUDGET_EXCEEDED` |
| Extracted text, running total over the pages | 48 MiB | `OCR_PARSE_BUDGET_EXCEEDED` |
| XObjects examined by the figure scan, per page / in total (counted from the raw `/XObject` dictionary before any member is resolved) | 1024 / 16384 | `OCR_PARSE_BUDGET_EXCEEDED` |
| stdout, drained incrementally | 64 MiB | `OCR_PARSE_BUDGET_EXCEEDED` |
| stderr, counted and discarded | 64 KiB | `OCR_PARSE_BUDGET_EXCEEDED` |
| `RLIMIT_FSIZE` = 0, `RLIMIT_NOFILE` = 32, `RLIMIT_CORE` = 0 | fixed | the write or open fails |
| Limits could not be applied; privileged process; Windows | - | `OCR_ISOLATION_UNAVAILABLE` |
| Malformed, non-canonical or unknown reply; worker crash | - | `OCR_PARSE_FAILED` |

`ProcessBudget` holds every value above, and an adapter's `budget=` argument overrides it.

### Decoding budget (pypdf)

Before the worker constructs a `PdfReader`, `_pdf_worker.apply_decode_limits` lowers every pypdf 6.x
module-level decompression limit to the budget and reads each one back (`DECODE_LIMITS`):

- `ZLIB_MAX_OUTPUT_LENGTH`, `LZW_MAX_OUTPUT_LENGTH`, `RUN_LENGTH_MAX_OUTPUT_LENGTH`,
  `JBIG2_MAX_OUTPUT_LENGTH`, `MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH`, `FLATE_MAX_BUFFER_SIZE`,
  `FLATE_MAX_ROW_LENGTH` and `ZLIB_MAX_RECOVERY_INPUT_LENGTH` are capped at `max_page_content_bytes`.
- `MAX_DECLARED_STREAM_LENGTH` is capped at the source budget (`max_input_bytes`).

A limit is never raised above pypdf's own default. A limit that pypdf disabled (0) gets the budget,
and a budget of 0 is refused, because 0 would disable the limit. If any name is missing or is not an
integer (a changed pypdf), parsing never starts (`OCR_PARSE_FAILED`). pypdf therefore stops a single
stream while decompressing it, as soon as it reaches the cap (`LimitReachedError`, reported as
`OCR_DECODE_BUDGET_EXCEEDED`). Object streams decompressed while the reader resolves objects are
covered too.

In the `text` operation, each page's content streams are decoded before `extract_text`. The
`/Contents` entry is read raw, and an array's length is checked before any stream in it is resolved.
Each stream is then decoded under the cap and the decoded sizes are summed against the per-page and
running budgets. pypdf caches the decoded data on the stream objects, and extraction reuses it. Only
then is the text extracted, measured and cut into lines. `CONTENT_DECODE_FACTOR = 8` exists because a
content stream carries operators, positioning and graphics in addition to the text it shows, so a
page's content may be several times its text. Eight times the text budget leaves room for
graphics-heavy pages and still bounds decoding to a fixed multiple of the text budget.

What remains bounded only by the process limits (stated precisely):

- `RLIMIT_AS` bounds peak allocation. Up to the cap, a stream is decoded into memory whole, because
  the pack does not stream-decode. Decoded content streams stay cached for the call (at most
  `max_content_bytes` in total). Filters without a pypdf output cap (`ASCIIHexDecode`,
  `ASCII85Decode`) expand at most 4x their input, which the source budget bounds. A chain of filters
  holds at most two capped stages in memory at a time.
- `RLIMIT_CPU` and the wall-clock deadline bound decoding work. That includes pypdf's zlib
  recovery loop and the parsing of an indirect `/XObject` dictionary, which must be parsed before its
  keys can be counted. None of its members is resolved before the count is checked.
- Form XObject content streams, decoded by `extract_text` itself, are each held to the per-stream cap
  but are NOT counted in the page or document content budgets (CR-15d OCR-17: the aggregate budget covers
  `/Contents` streams only; pypdf exposes no hook to charge every stream extraction visits). The worker's
  `RLIMIT_AS`, `RLIMIT_CPU` and wall-clock deadline remain the bound on total decoding. Python's recursion limit bounds nesting, and pypdf maps
  `RecursionError` to a read error.

### Containment guarantee

On any overrun, and again after a normal exit, the runner SIGKILLs the child's **original process
group** (`CONTAINMENT_GUARANTEE = "ORIGINAL_PROCESS_GROUP"`). On its own, that reaches the child and
**descendants that stay in the original process group**; a descendant that calls `setsid()` or
`setpgid()` would leave it. Since CR-15c no such descendant can exist: every child (the PDF worker, the
binding worker and the Tesseract launcher, including the binary it execs) runs with `RLIMIT_NPROC = 0`,
which is mandatory. `deny_process_creation` no longer exists, and the limits token has no other
`nproc` value. A privileged process is refused before anything starts (step 1). The guarantee is
therefore: **an unprivileged child, with no new privileges (Linux), that can create no process and no
thread, killed by its process group.** An engine build that needs a thread or a process fails closed
(`ENGINE_FAILED`). For real engines that remains to be established live (`OCR_LIVE_ENGINE_UAT =
PENDING`).

`RLIMIT_NPROC` semantics: Linux counts every process and every thread of the REAL UID, system-wide,
against the limit, and checks it at `fork`/`clone`. So a positive value does not mean "N children of
this call": it is compared with everything the uid already runs, and so bounds nothing per call. That
is why 0 is the only value the pack uses. The limit is not enforced for real uid 0 or for a process
with `CAP_SYS_RESOURCE` or `CAP_SYS_ADMIN`, which is why such a process launches nothing. Every PDF
test runs the real worker under `RLIMIT_NPROC = 0`. pypdf creates no thread and no process.

Nothing is buffered without a cap (no `capture_output`, no `communicate`), and the pack creates no thread
or timer. The worker's closed refusal codes (`PDF_ENCRYPTED`, `PDF_UNREADABLE`, `PAGE_RANGE_OUT_OF_BOUNDS`,
`PAGE_COUNT_UNSUPPORTED`, `FIGURES_TOO_MANY`, `MANIFEST_PAGE_COUNT_MISMATCH`, `OCR_PARSE_BUDGET_EXCEEDED`,
`OCR_DECODE_BUDGET_EXCEEDED`) pass through. Anything else becomes `OCR_PARSE_FAILED`. Results are the same
pypdf calls as before. Only `_isolated.py` creates processes. Only `_rlimit_exec.py` sets limits, execs
or uses `ctypes` (for `prctl`, in the child). Only `_binding_worker.py` imports an operator module.
Static tests enforce all three.

### Engine bindings (PaddleOCR, MinerU-style)

An `EngineBinding` is operator configuration, not an object: `target = "package.module:function"`, plus
`timeout_seconds` (default 300), `memory_mb` (4096), `max_output_bytes` (16 MiB), `max_stderr_bytes`
(64 KiB) and `max_open_files` (256). The pack never imports or calls it in the PADOS process, and an
in-process object is refused (`TypeError: ENGINE_BINDING_INVALID`). `isolated_binding` runs it as
`python -I -B -m pados.packs.ocr._binding_worker <limits> <module:function>`. The worker applies the
limits first (step 4), then reads the header (`media_type`, `pages`, `languages`) and the image bytes
from stdin. Only after that does it import the operator module and call
`function(payload, *, media_type, pages, languages)`, which returns `ocr.engine-result/v1` as canonical
JSON. The environment is minimal, and native thread pools are pinned to one thread (`OMP_NUM_THREADS`,
`OPENBLAS_NUM_THREADS`, `MKL_NUM_THREADS` = 1); creating a thread fails anyway. So every decoder and
every model the binding uses runs inside the bounded child. The pack's header validation (side and
pixel budgets) still runs before it.

Codes: `ENGINE_TIMEOUT`, `ENGINE_BUDGET_EXCEEDED` (`MemoryError` under `RLIMIT_AS`, or a kernel-enforced
limit), `ENGINE_OUTPUT_TOO_LARGE`, `ENGINE_OUTPUT_INVALID` (not a JSON object), and
`ENGINE_BINDING_IMPORT_FAILED` (the module or function cannot be imported in the isolated interpreter).
A binding reports its own closed code by raising `_binding_worker.BindingRefusal(code)` with a code in
`BINDING_CODES`. Anything else becomes `ENGINE_FAILED`, and no exception text leaves the child.

Installation: `-I` ignores `PYTHONPATH`, the user site-packages and the working directory, so the
operator module **must be installed into the PADOS environment's site-packages** (a regular install or
a `.pth` there). `available()` checks only that the engine package and the binding's top-level package
are findable, without executing anything. The authoritative check is the import in the child.
The tests do exactly this: a TEST-ONLY module in `tests/fixtures/ocr/bindings/` is made importable by a
temporary `.pth` file in the venv's site-packages, which the test removes afterwards (if site-packages
is not writable, those tests skip). A binding that allocates past its limit is stopped
(`ENGINE_BUDGET_EXCEEDED`), and a normal one returns a result.

Vision models (`VisionModelOcrAdapter`) are different: the adapter is a MODEL_RUNTIME delegate, and its
`VisionModelBinding` is a bridge to the worker that the Worker Router selected. Containment of that
model runtime, and of any decoder it runs, is the router-side worker's. This pack does not provide it.
The bridge's failures are still closed codes with no exception chain.

### Windows: fail closed

On Windows (`sys.platform == "win32"`) the runner spawns nothing and raises `OCR_ISOLATION_UNAVAILABLE`
(`isolation_refusal() == "WINDOWS_NO_JOB_OBJECT"`). `PdfTextLayerAdapter`, `DocumentPreprocessAdapter`
(whole adapter, images included), `XObjectFigureAdapter`, `TesseractCliAdapter`, `PaddleOcrAdapter` and
`MinerUStyleAdapter` report `available() == False` with `unavailable_reason() ==
"OCR_ISOLATION_UNAVAILABLE"`, so the shared policy refuses them (`ADAPTER_UNAVAILABLE`). Called directly,
they refuse before reading any input. The earlier temporary-file polling branch has been removed. Pure
Python stages (layout, tables, structure) are unaffected.

Design for real containment (`WINDOWS_NATIVE_VALIDATION = PENDING`; NOT implemented, no Windows code ships):
create the child suspended (`CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`, no inherited
handles except the three pipes), create a Job Object with ctypes (`CreateJobObjectW`), and set
`JOBOBJECT_EXTENDED_LIMIT_INFORMATION` with `JOB_OBJECT_LIMIT_PROCESS_MEMORY` (= `memory_bytes`),
`JOB_OBJECT_LIMIT_ACTIVE_PROCESS` = 1 (no descendants; the analogue of `RLIMIT_NPROC = 0`) and
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Add `JOBOBJECT_CPU_RATE_CONTROL_INFORMATION` (hard cap) plus a
per-job user-time limit (`JOB_OBJECT_LIMIT_JOB_TIME`) for the CPU budget. Assign the process
(`AssignProcessToJobObject`) BEFORE `ResumeThread`, and fail closed if any call fails. Drain stdout and
stderr through bounded anonymous pipes with overlapped reads and the same caps (no temporary files),
take the deadline before `CreateProcess`, and on any overrun or after exit call `TerminateJobObject`
and close the job handle. That kills the whole job, descendants included, whatever process group they
claim. It must be executed and tested natively on Windows before `available()` may become True there.

### Image inputs

PNG and JPEG need a parseable header with validated dimensions before anything else runs: each side at most
`MAX_IMAGE_SIDE` = 20000 px and the total at most `MAX_IMAGE_PIXELS` = 100 MP. Otherwise preprocess fails
with `IMAGE_HEADER_INVALID`, `IMAGE_DIMENSIONS_EXCEEDED` or `IMAGE_PIXELS_EXCEEDED`. The
`ocr.preprocessed/v1` model enforces the same budgets, so no recognition stage can receive an image manifest
without validated pixel geometry. **TIFF is refused in V1** (`IMAGE_TIFF_UNSUPPORTED`), because the pack does
not validate TIFF dimensions or frame counts. The media type stays in the task contract so a later version
can add a validator.

### Errors carry no content

At the public boundaries (`OcrStageAdapter.execute`, engine and vision bindings, the pipeline runner and
lineage), a failure is a new exception with a closed code, raised outside any handler. It has no
`__cause__` and no `__context__`, so an internal exception, a pydantic `ValidationError` that quotes input
values, or engine output can never ride along. Closed means REGISTERED, not merely uppercase. A stage
code must be in the explicit registry `_stage.STAGE_CODES`, and a static test checks that every code the
pack raises is in it. Anything else becomes `OCR_STAGE_FAILED`, with `retryable` cleared, even an
uppercase document identifier or a secret shaped like a code. The runner applies the same registry to
codes from adapters that are not `OcrStageAdapter`s. It maps any other exception from such an adapter to
`OCR_STAGE_FAILED`, and binding refusals outside the shared contract's own codes to `STAGE_BINDING_REFUSED`.

## Platform residuals

- **Windows (`WINDOWS_NATIVE_VALIDATION = PENDING`).** Fail closed (see "Windows: fail closed"): no PDF
  parsing, no preprocess, no Tesseract and no engine bindings on Windows until the Job Object design
  above is implemented and validated natively. `mypy --platform win32` is clean, and a test
  monkeypatches `sys.platform` to prove that nothing is spawned. Windows parity is **not** claimed.
- **POSIX containment.** The guarantee is an unprivileged child that cannot create a process or a
  thread, killed by its original process group (above). On Linux it also has no new privileges. It
  depends on the kernel honouring `RLIMIT_NPROC` for an unprivileged uid, and the pack refuses to launch
  anything when it cannot establish that the process is unprivileged. `RLIMIT_AS` limits address
  space, not resident memory. The group kill after a normal exit could in theory hit a reused
  process-group id in the instant after the leader is reaped. rlimits do not isolate the filesystem or
  the network: an engine binary or binding can still read what the uid can read and open sockets.
  `PR_SET_NO_NEW_PRIVS` is Linux-only; on other POSIX systems only the uid check and the
  setuid/setgid refusal of the configured binary apply.
- **Startup.** Starting a child is a plain fork+exec of the PADOS interpreter, with no Python in
  between. The only point at which the PADOS process can block without a deadline is that exec
  inside `Popen`. An exec that blocks in the kernel, for example a binary on a hung network
  filesystem, cannot be interrupted without a thread, and the pack creates none. The validated, local,
  regular file (the interpreter's resolved file; the operator binary is exec'd by the launcher after
  `Popen` has returned, inside the deadline) mitigates this. It does not remove it. If `Popen` returns
  late, the child is killed at once and the call times out. The interpreter's own `bin/python`
  symlink is accepted by design: its resolved file is what is validated. The configured binary is
  validated three times (readiness, before the spawn, in the launcher right before `execv`), but a
  writer with access to its directory could still swap it between the last check and the `execv`.
  Keep operator binaries in directories that only the operator can write.
- **Decoding.** See "Decoding budget": peak allocation is bounded by `RLIMIT_AS`, and decoding work by
  `RLIMIT_CPU` and the deadline. The pack does not stream-decode, and Form XObject streams are capped
  per stream, not per page.
- **Engine bindings and real engines.** `OCR_LIVE_ENGINE_UAT = PENDING`. No real Tesseract, PaddleOCR or
  MinerU build has been run under `RLIMIT_NPROC = 0`, `RLIMIT_FSIZE = 0` and the default memory budget.
  Builds that need threads, worker processes or cache-file writes fail closed (`ENGINE_FAILED` or
  `ENGINE_BUDGET_EXCEEDED`). Engine decoders run inside the bounded child, and the pack validates only
  the declared image header before them. Vision-model containment is router-side (above).
- **Exit-code attribution.** A launched binary that itself exits 117 is reported as
  `OCR_ISOLATION_UNAVAILABLE`, and one that exits 119 as `ENGINE_EXECUTABLE_INVALID` (still a failure;
  nothing is accepted).

## Data kinds requested from the integrator (not added by this pack)

| DataKind (proposed) | Retention | Content |
| --- | --- | --- |
| `OCR_INTERMEDIATE` | R0_EPHEMERAL | preprocess manifests and intermediate `ocr.document/v1` artifacts |
| `NORMALIZED_DOCUMENT` | R2_PROJECT | the final `ocr.document/v1` artifact (document text; project data) |
| `OCR_STAGE_EVIDENCE` | R1_OPERATIONAL | stage receipts and evidence (hashes, counts, confidences; no content) |

## Known limits (V1)

- Preprocess validates the input and records page selection and geometry. It does not deskew, binarize or
  rasterize, because V1 has no image engine.
- Text-layer blocks have no bbox. Layout groups lines by their grid position, and reading order is
  single-column.
- Table detection is whitespace-aligned only. Figures are listed by reference, and their bytes are not
  extracted.

## CR-15c remediation status (fixer cycle 3)

| Finding | What changed | Residual (see "Platform residuals") |
| --- | --- | --- |
| OCR-14 | `is_registered_preprocess` requires the exact type, the registered digest, no instance-level method overrides and a `ProcessBudget` | Code in the PADOS process that patches the class itself |
| OCR-15 / OCR-12 | `MEASUREMENT_POLICY` (unweighted LINE mean and minimum), computed from LINE blocks only and carried as `PipelineResult.measurements` | None known. `page_summary` was already LINE-only, so the regression test also passes on the pre-fix code |
| OCR-02 / OCR-10 | `RLIMIT_NPROC = 0` is mandatory (`deny_process_creation` no longer exists); privileged processes launch nothing; the child re-checks and sets `PR_SET_NO_NEW_PRIVS` (Linux) | Filesystem and network are not isolated; real engines must work without threads (`OCR_LIVE_ENGINE_UAT = PENDING`) |
| OCR-01 | pypdf decompression limits are set from the budget; content streams are decoded under the per-page and total caps before `extract_text`; XObjects are counted from the raw dictionary | No stream decoding; `RLIMIT_AS` bounds peak allocation, and CPU and the deadline bound the work |
| OCR-08 | Exec targets are validated (no symlinks, regular, executable, not setid, no file capabilities); a late `Popen` kills at once; reaping is bounded | An exec that blocks inside the kernel cannot be interrupted without threads |
| OCR-03 | `EngineBinding` is a `module:function` run only in the bounded binding worker; the in-process call path is removed | Vision-model containment is router-side; real bindings are `OCR_LIVE_ENGINE_UAT = PENDING` |
| Windows | Unchanged: fail closed | `WINDOWS_NATIVE_VALIDATION = PENDING` |

## CR-15d status (after the third and final fixer cycle; Codex final verification)

FIXED: OCR-02, OCR-05, OCR-12, OCR-14, OCR-15 (and OCR-04, 06, 07, 09, 11, 13 earlier).

| Finding | Status | Disposition |
| --- | --- | --- |
| OCR-16 (HIGH, new) | Closed by integrator mitigation | `privilege_refusal_for` now refuses ANY retained capability in the effective, permitted or ambient set (`CAPABILITIES_HELD`), not only `CAP_SYS_RESOURCE`/`CAP_SYS_ADMIN`; `CAP_SETUID` and similar can no longer pass |
| OCR-17 (MEDIUM, new) | Claim narrowed | The aggregate decoded-content budget covers page `/Contents` streams only; Form XObjects are per-stream capped and bounded by the worker's rlimits and deadline |
| OCR-01 (HIGH) | Partial: library limit | No streaming extraction in pypdf; complete text/stream materialization is bounded by `RLIMIT_AS`, `RLIMIT_CPU` and the deadline, not by the text budget |
| OCR-03 (HIGH) | Partial: engine-side | Header-level image budgets only; decoders run inside the bounded binding worker (memory-limited), with no decoder-level geometry validation; the vision bridge is a Worker Router-delegated model runtime whose containment is router-side |
| OCR-08 (HIGH) | Partial: OS limit | A `Popen` blocked inside the kernel's `exec` cannot be interrupted without a supervisor; the validated local regular file reduces this; a hard startup deadline would need an independently supervised launcher |
| OCR-10 (HIGH) | Partial: OS limit | rlimits are not a sandbox; containment relies on an unprivileged, capability-free child with `RLIMIT_NPROC=0` and process-group cleanup; Linux-validated only |

These partial items are documented OS/library limits of an rlimit-based design, not configurable bypasses. Closing them
fully requires an OS sandbox (namespaces/seccomp/cgroups on Linux, Job Objects on Windows), which would be a new
runtime dependency or platform component (`OCR_SANDBOX_RUNTIME = USER_DECISION_REQUIRED`).
