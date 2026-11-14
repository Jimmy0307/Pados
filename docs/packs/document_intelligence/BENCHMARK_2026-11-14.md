# Document Intelligence benchmark (2026-11-14)

**Synthetic, public-safe, small. Not evidence of production quality.**

- Corpus: `pados.document-intelligence.synthetic-ncr.v1` (`tests/fixtures/document_intelligence/corpus.py`):
  six invented nonconformance reports, 66 passages and 20 English queries (9 keyword, 11 paraphrase). Judgments
  are graded (2 = states the answer, 1 = related) and were written together with the corpus by the same author.
- Command: `python scripts/benchmarks/document_intelligence_benchmark.py --out <dir>`. It needs the pinned models
  (`scripts/models/fetch_pinned_models.py`).
- Commit: `12765a4d638b435a9561064d14027914ec68f534` (clean tree) on both platforms. After the review fixes
  (`8249c83`: SHADOW order, ACTIVE ties, engine deadlines), a Windows re-run gave identical quality, grounding and
  probe results. Runs at the final PR head are listed in the PR evidence.
- Raw outputs are kept outside the repository (the evidence directory of this round):
  - Windows `document-intelligence-benchmark.json`, SHA-256
    `bab7e03b122fc4a7d19dccb2e6ed183c980e9d2c0d307c4473340551039a8ddc`.
  - VMware Linux, SHA-256 `06211b0604a0cc1420d59b447f2f1e1abed06fd32ade6402acad376c9acd9a75`.
- Models: `bge-small-en-v1.5-onnx-q` (pin digest `704dcf03…`) and `ms-marco-minilm-l6-v2-onnx` (pin digest
  `6c0a15fd…`). Engine: `fastembed@0.9.1+onnxruntime@1.32.0:cpu`.
- Configuration: BM25 k1 = 1.2, b = 0.75. Candidate pool 20 for BM25, dense and fusion. RRF k = 60, equal
  weights. Cross-encoder ACTIVE over the fused top 20, keeping the top 8. Each query is ONE governed task, planned
  and executed by the existing fabric; the four rankings come from that task's stage receipts.
- Recognition is FIXTURE-provenance (engine-result path). **OCR quality is not measured here.** The text-layer
  fidelity of the PDF path is a separate Linux test (`test_di_pdf_chain.py`).

## Retrieval quality (mean over 20 queries; identical on Windows and Linux)

| Method | Success@1 | Success@5 | Recall@5 | MRR@8 | nDCG@5 |
| --- | --- | --- | --- | --- | --- |
| BM25 only | 0.40 | 0.50 | 0.458 | 0.435 | 0.436 |
| Embedding only | 0.40 | 0.75 | **0.692** | 0.526 | 0.569 |
| Hybrid (RRF) | 0.35 | 0.65 | 0.558 | 0.463 | 0.475 |
| Hybrid + cross-encoder | 0.40 | **0.85** | 0.658 | **0.528** | **0.573** |

By query style (nDCG@5 / Success@1):

| Method | Keyword (9) | Paraphrase (11) |
| --- | --- | --- |
| BM25 only | **0.748** / **0.67** | 0.180 / 0.18 |
| Embedding only | 0.678 / 0.56 | 0.480 / 0.27 |
| Hybrid (RRF) | 0.700 / **0.67** | 0.291 / 0.09 |
| Hybrid + cross-encoder | 0.618 / 0.44 | **0.537** / **0.36** |

Reading the numbers:

- BM25 is the best method for keyword queries (part numbers, NCR ids) and nearly useless for paraphrases.
- Dense retrieval helps paraphrase queries most (nDCG@5 0.18 → 0.48).
- **Equal-weight RRF did worse than dense alone.** On paraphrase queries the weak BM25 list drags good dense
  hits down. Do not make hybrid the default on this evidence.
- The cross-encoder over the hybrid pool gave the best overall Success@5 (0.85) and nDCG@5 (0.573). Its gain over
  dense alone is small (+0.004 nDCG@5, +0.002 MRR). It **hurt keyword queries**: Success@1 fell from 0.67 (BM25,
  hybrid) to 0.44.
- Decision: the rerank templates stay **SHADOW** by default. ACTIVE stays a separate, operator-chosen template. The
  engines stay an optional extra.

## Latency and resources (governed path, per query)

| | Windows 11 (32 logical CPUs, 31.7 GiB) | VMware Ubuntu 24.04 (4 vCPU, 7.7 GiB) |
| --- | --- | --- |
| Model warm-up (both models, first call) | 1035 ms | 1739 ms |
| BM25 stage (median) | 232 ms | 11 ms |
| Dense stage (median) | 1841 ms | 718 ms |
| Fusion stage (median) | 533 ms | 13 ms |
| Rerank stage (median, 20 candidates) | 569 ms | 124 ms |
| Whole task (median, after the first) | 3164 ms | 869 ms |
| Process RSS before → after | 330 → 411 MiB (peak working set 667 MiB) | 414 → 473 MiB |

- Stage times are receipt `duration_ms`, which covers the governed work: the artifact store, schema checks,
  and the replay verification of deterministic stages.
- The dense stage re-embeds every passage on every query. There is no embedding cache or index (DI-3).
- Deterministic stages cost about 20 to 40 times more on Windows than on Linux, with no model involved (BM25 232
  vs 11 ms, fusion 533 vs 13 ms). The overhead sits in the governed stage path on Windows, not in the engines.
  It is recorded as DI-9 and has not been profiled yet.

## Grounded workflow (two governed tasks per document, hybrid + SHADOW rerank, top 8 → evidence)

| Document | Evidence items (structured) | Report | Sections | Grounded citations correct |
| --- | --- | --- | --- | --- |
| NCR-2041 | 8 (7) | INCOMPLETE | D3 abstained, 7 supported | 7 / 7 |
| NCR-2057 | 8 (7) | INCOMPLETE | D7 abstained | 7 / 7 |
| NCR-2063 | 8 (7) | INCOMPLETE | D6 abstained | 7 / 7 |
| NCR-2079 | 8 (7) | INCOMPLETE | D7 abstained | 7 / 7 |
| NCR-2091 (no root cause yet) | 7 (3) | INCOMPLETE | D4–D8 abstained | 3 / 3 |
| NCR-2095 (two root causes) | 7 (4) | REVIEW_REQUIRED | D4 CONFLICTED | 2 / 2 |

- "Correct" means the cited passage itself states the claim's (subject, assertion), checked by re-reading the
  corpus text with the evidence stage's token normalization.
- A first run at `a919d5b` reported 5/7, 4/7 and 0/3. That was a defect in the **benchmark's checker**: it
  compared raw text with tokens. The output is kept, and the fix is `12765a4`.
- Each complete NCR abstains on exactly one section, because its labelled line ranked below the top-8 cut-off.
  The pipeline never fills a section without grounded evidence, so the report is INCOMPLETE rather than wrong.
- The claim provider is the deterministic extractive drafter. A model claim provider was not measured.

## Refusal probes (all as designed, both platforms)

| Probe | Result |
| --- | --- |
| Re-register a project-A document for project B | `INPUT_PROVENANCE_ENTRY_CONFLICT` |
| Project-A task over a project-B document | plan `INFEASIBLE` |
| Stale pinned evidence digest | `solve:PS_EVIDENCE_DIGEST_STALE` |
| Evidence source not authorized by the task | `solve:PS_EVIDENCE_SOURCE_UNAUTHORIZED` |
| Malformed `ocr.document/v1` | `retrieve:RETRIEVAL_DOCUMENT_INVALID` |
| Embedding engine not installed | hybrid plan `INFEASIBLE` (no fallback) |

## Limitations

- Six invented documents in one domain and one language. The judgments come from the corpus author, and there
  is one run per platform.
- These results do not predict quality on real documents, other languages or scanned input.
