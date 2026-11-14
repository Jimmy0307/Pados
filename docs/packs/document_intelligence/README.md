# Document Intelligence Packs

Status: **EXPERIMENTAL**, stacked on PR #22. Local supervised dogfood only; production NOT_READY.

The Document Intelligence packs turn the retrieval capabilities that were contract-only into real, governed
execution paths. They also connect a ranked document to the existing Structured Problem Solving pack and the
existing `ResultCandidate` path. They add no scheduler, router, executor, registry, receipt type, store, CAS,
approval authority or task model. Every stage is a Capability Pack V1 manifest registered through the existing
`CapabilityRegistry`. Each stage is planned by the existing `CapabilityPlanner`, runs only through
`CapabilityExecutor` → `run_bound_stage`, and is verified by the existing `VerificationEngine`.

## Workflow

```mermaid
flowchart LR
  subgraph T1["Task 1: document_evidence.&lt;start&gt;.&lt;retrieval&gt; (existing planner + executor)"]
    PDF[PDF bytes] -->|Linux: isolated OCR pack| OCR[preprocess / recognize / layout / tables / structure]
    DOC[ocr.document/v1] --> BM25
    OCR --> DOC
    BM25[PASSAGE_RETRIEVAL<br/>BM25, existing] --> FUSE
    DOC --> DENSE[PASSAGE_RETRIEVAL_DENSE<br/>pinned bge-small] --> FUSE[PASSAGE_RANK_FUSION<br/>RRF, enterprise formula]
    FUSE --> RR[PASSAGE_RERANK_CROSS_ENCODER<br/>SHADOW / ACTIVE, existing rerank()]
    RR --> EV[PASSAGE_EVIDENCE_PACK<br/>evidence pack + evidence map]
  end
  EV -->|admit_verified_evidence<br/>operator step, provenance all-or-nothing| PIN[Task pins pack digest<br/>and authorizes source]
  subgraph T2["Task 2: problem_solving.document"]
    PIN --> PS[STRUCTURED_PROBLEM_SOLVING<br/>existing pack: admit, ground, abstain]
  end
  PS -->|verified report| RC[result_projection → normalize → coverage → compose<br/>existing work_protocol]
  RC --> CAND[ResultCandidate<br/>PENDING_GOVERNED_ACCEPTANCE]
  CAND -.->|existing governed acceptance + CAS only| CAS[(CAS)]
```

| Capability | Adapter | Determinism | Verification | Notes |
| --- | --- | --- | --- | --- |
| `PASSAGE_RETRIEVAL` (existing) | `retrieval.lexical-bm25` | DETERMINISTIC | SCHEMA, REPLAY | unchanged |
| `PASSAGE_RETRIEVAL_DENSE` | `document-intelligence.dense-fastembed` | PROBABILISTIC | SCHEMA, REFERENCE_CHECK (model identity) | pinned `BAAI/bge-small-en-v1.5` (384-d) |
| `PASSAGE_RANK_FUSION` | `document-intelligence.rrf-fusion` | DETERMINISTIC | SCHEMA, REPLAY | `enterprise.pipeline.reciprocal_rank_fusion` (k=60) |
| `PASSAGE_RERANK_CROSS_ENCODER` | `document-intelligence.cross-encoder-fastembed` | PROBABILISTIC | SCHEMA, REFERENCE_CHECK | pinned `ms-marco-MiniLM-L-6-v2`; mode is fixed by the operator template |
| `PASSAGE_EVIDENCE_PACK` | `document-intelligence.passage-evidence` | DETERMINISTIC | SCHEMA, REPLAY | `problem_solving.evidence-pack/v1` + evidence map |
| `STRUCTURED_PROBLEM_SOLVING` (existing) | `problem-solving.grounded` | PROBABILISTIC | SCHEMA, REFERENCE_CHECK (re-derivation) | unchanged |

Templates (`pados.packs.document_intelligence.wiring`): `document_evidence.{pdf,document}.{lexical,hybrid,hybrid_rerank}`
use SHADOW rerank. `…hybrid_rerank.active` uses ACTIVE rerank. `problem_solving.document` is the grounding task.
A PDF start runs the existing OCR-pack stages, which run only on POSIX (R19-3). A document start takes an
`ocr.document/v1`, for example one produced by the OCR pack on a Linux worker.

## Contracts

- `retrieval.scored-ranking/v1` (`ranking.py`): passages are named by block id, section, page and code-point
  offsets in the BM25 reading-order view. A ranking never carries document or query text. The query is
  identified by the **same hash the BM25 stage records**, so a fusion or rerank stage refuses a ranking of another
  document (`DI_RANKING_DOCUMENT_MISMATCH`) or another query (`DI_RANKING_QUERY_MISMATCH`). Model rankings name
  the model pin, its digest and the engine identity. A fusion ranking names its input ranking digests. A rerank
  ranking records its mode and outcome (`APPLIED`, `SHADOW_SCORED` or `SHADOW_FAILED`). In SHADOW mode the
  published order is the prior one and `shadow_order` is the reranker's own top-k over all candidates (what ACTIVE
  would publish). ACTIVE ties keep the prior order. Dense scores must lie in the cosine range.
- Evidence hand-off (`evidence.py`): a passage line `<Label>: <value>` whose label normalizes to a subject token of
  the chosen profile (`Root cause:` → `root_cause`) becomes a comparable (subject, assertion) evidence item. Every
  other passage becomes a statement-only item, which can be displayed but can never ground a claim.
  `content_digest` is the SHA-256 of the passage text, so a changed document yields a different, stale pack.
  Evidence ids are `ev.<rank>.<n>`, which is bounded whatever the block id. The evidence map binds each item to
  its passage, page, offsets and line. The evidence stage binds its ranking to the document but takes no query: it
  packages whatever final ranking the template feeds it.
- **One document is one case.** Every item of a pack is attributed to the task's `case_id`. A multi-case binder
  must be split into per-case documents first. The retrieval benchmark uses a binder; the grounding workflow never
  does.

## Model engines and pins

`model_pin.py` + `local_models.py`. FastEmbed 0.9.x (Apache-2.0) over ONNX Runtime (MIT), CPU execution provider.
It is an optional extra: `pip install "pados[document-intelligence]"`.

| Pin | Source revision | Upstream license | Files pinned | Bounds |
| --- | --- | --- | --- | --- |
| `bge-small-en-v1.5-onnx-q` | `Qdrant/bge-small-en-v1.5-onnx-Q@aa8f8b06` (weights of `BAAI/bge-small-en-v1.5`) | MIT | ONNX + tokenizer + configs (SHA-256 + size) | batch 32, 2000 chars, 60 s, 2 threads, 1 GiB |
| `ms-marco-minilm-l6-v2-onnx` | `Xenova/ms-marco-MiniLM-L-6-v2@a0914435` (weights of `cross-encoder/ms-marco-MiniLM-L-6-v2`) | Apache-2.0 | ONNX + tokenizer + configs | batch 16, 2000 chars, 60 s, 2 threads, 1 GiB |

- **No download by PADOS.** The engine loads only with `specific_model_path`, `local_files_only` and
  `HF_HUB_OFFLINE=1`, and only after `verify_model_dir` matches every pinned file byte for byte. A live test loads
  both engines with sockets blocked. Symlinks, path escapes, resized or altered files and **any unpinned file**
  are refused; the fetch tool's `.cache` metadata is the one exception. Verification is cached on a stat
  fingerprint of the directory: any change re-verifies and unloads the engine. The operator fetch is explicit (`scripts/models/fetch_pinned_models.py --approve <pin>
  --accept-license <SPDX>`); a mismatch is quarantined. The ONNX digests equal the Hugging Face LFS SHA-256
  records.
- Only permissive licenses are pinnable (MIT, Apache-2.0, BSD-2/3). `jina-reranker-v2` (CC-BY-NC-4.0) is refused
  by construction.
- Every call is bounded: at most 4096 texts and 1,000,000 characters (`LOCAL_MODEL_INPUT_TOO_LARGE`), with each
  input cut to the pin's length.
- Each batch runs on a daemon thread under the pin's deadline, and a model load gets five times that deadline.
  CPython cannot stop the thread, so a late batch makes the engine refuse (`ENGINE_POISONED`) only until that
  computation returns; the engine then reloads. A runaway thread never blocks interpreter exit.
- Outputs are validated for count, dimension and finiteness, L2-normalized and quantized to 6 decimals.
- The model manifests' `execution_config_digest` binds the pin digest, engine label, providers, threads and
  bounds (Measured Capability: MODEL × RUNTIME × DEVICE × CONFIG × EVIDENCE).

## Failure semantics

| Situation | Behaviour |
| --- | --- |
| Engine not installed / model dir missing or altered | adapter unavailable → the hybrid plan is **INFEASIBLE**. There is no silent fallback; the lexical template remains a separate choice |
| Engine exception | `LOCAL_MODEL_FAILED`, empty exception chain, no text echoed |
| Batch past the deadline | `LOCAL_MODEL_TIMEOUT` (the only retryable code); the engine refuses until the runaway batch returns |
| Input above 1,000,000 characters | `LOCAL_MODEL_INPUT_TOO_LARGE` |
| Unpinned file in the model directory | `MODEL_DIR_UNPINNED_FILE`; the engine is unavailable |
| ACTIVE rerank failure | stage fails closed (`DI_RERANK_FAILED`), no evidence produced |
| SHADOW rerank failure | prior order published, `rerank_outcome = SHADOW_FAILED`, `model_score = null` |
| Ranking of another document or query | `DI_RANKING_DOCUMENT_MISMATCH` / `DI_RANKING_QUERY_MISMATCH` |
| Malformed `ocr.document/v1` | the BM25 stage fails closed (`RETRIEVAL_DOCUMENT_INVALID`) |
| Stale evidence digest / unauthorized source | existing `PS_EVIDENCE_DIGEST_STALE` / `PS_EVIDENCE_SOURCE_UNAUTHORIZED` |
| Another project's document | plan not feasible (existing input provenance) |
| Missing evidence | the existing pipeline ABSTAINS for the section; the report is INCOMPLETE or ABSTAINED |
| Conflicting evidence | CONFLICTED → REVIEW_REQUIRED; never resolved by the pipeline |
| Fabricated or uncited claims | UNSUPPORTED / CONTRADICTED; never promoted to a SUPPORTED section |

## Security boundary

- Every pack declares `egress = NONE`, `LOCAL_ONLY` and `semantic_authority = NONE`. The model stages cap at
  CONFIDENTIAL; the deterministic stages allow RESTRICTED.
- **In-process third-party runtime (residual DI-1).** ONNX Runtime parses the pinned model file inside the PADOS
  process. Mitigations: in-tree pack code; the model bytes are pinned and verified before load; CPU provider only;
  no network. A `LOCAL_SUBPROCESS` worker would isolate a runtime crash or a malicious model file, and it would
  also enforce a real kill and the pin's `max_ram_mb`, which is declared but not enforced in-process. Deciding
  that is an owner decision (see register).
- **Hash-then-load window (residual DI-2).** The engine reopens the files by path after they are hashed. Keep the
  model root writable only by the operator.
- Authorization precedes ranking. A stage reads only the scoped artifacts the fabric bound to it, so a reranker
  never sees text outside the task's authorized inputs.
- The evidence statement is bounded display text (printable, ≤ 280 chars), never an instruction.

## Reuse audit (what was reused instead of rebuilt)

| Need | Classification before | Reused | New |
| --- | --- | --- | --- |
| PDF text layer, OCR stages, malformed-PDF budgets | ALREADY_IMPLEMENTED (POSIX), fails closed on win32 | OCR pack, unchanged | — |
| Tesseract / PaddleOCR | ENGINE_NOT_INSTALLED, VALIDATED_SYNTHETIC | — | none (see OCR status) |
| Canonical document + passages | ALREADY_IMPLEMENTED | `ocr.document/v1`, `lexical.passages` | — |
| BM25 | ALREADY_IMPLEMENTED | `PASSAGE_RETRIEVAL` | — |
| Embedding retrieval | CONTRACT_ONLY | registry, executor, receipts | FastEmbed adapter + pins |
| Hybrid fusion | ALREADY_IMPLEMENTED (enterprise stack only) | `_fuse` → public `reciprocal_rank_fusion` | pack stage |
| Rerank modes | ALREADY_IMPLEMENTED (`enterprise.rerank`) | `Reranker`, `rerank()` | cross-encoder engine |
| Evidence grounding, abstention, conflicts | ALREADY_IMPLEMENTED (problem-solving pack) | unchanged | passage → evidence-pack stage |
| Stale digest, source authorization, cross-project | ALREADY_IMPLEMENTED | `admit_evidence`, input provenance | — |
| ResultCandidate | ALREADY_IMPLEMENTED | work-protocol functions | `to_result_candidate` wrapper |
| Pack registration, evidence and closed codes | ALREADY_IMPLEMENTED | `register_pack`, `GovernedPackAdapter` | 3 manifests |
| Wiring | ALREADY_IMPLEMENTED | `wire_pipeline` | additive `extensions` hook |
| pgvector persistence | CONTRACT_ONLY (placeholder columns) | — | **not introduced**: per-document ranking embeds ≤ 4095 passages per call, so no persistence was needed for the measured use |

## Residuals

Recorded in `docs/audits/PADOS_OPERABILITY_RESIDUAL_REGISTER.md` (DI-1 … DI-8).
