# Document Intelligence evaluation protocol

Purpose: decide retrieval and grounding defaults from evidence that is independent of the people who built the
pipeline, while keeping synthetic measurements clearly apart from real-corpus ones.

## Current decision (2026-11-14, synthetic evidence only)

- Dense retrieval is the preferred **measured** baseline: nDCG@5 0.569 against 0.436 for BM25.
- Rerank stays **SHADOW**. It gave the best Success@5 (0.85) but lowered keyword Success@1 from 0.67 to 0.44.
  ACTIVE is never the default.
- Hybrid (equal-weight RRF) is not the default, because it measured below dense alone (0.475).
- These decisions hold until an evaluation of the kind below exists. A synthetic result never upgrades them.

## Two tracks that never mix

| | Track S (synthetic) | Track R (real) |
| --- | --- | --- |
| Corpus | Generated in-repo (`tests/fixtures/document_intelligence/corpus.py`), public-safe | Owner-approved documents, held outside the repository (classification and ACL recorded) |
| Judgments | Written with the corpus | Written **before** any system output is seen, by a judge other than the implementer, on a graded 0–2 scale; 20% double-judged, agreement (Cohen's κ) reported |
| Purpose | Regression and smoke tests: did the pipeline break? | Default selection: is a method better? |
| Reports | Labelled `SYNTHETIC` in every table | Labelled `REAL`, with corpus id, size, languages and date |

A report may cite both tracks, never in the same table or average.

## Query and document strata (each reported separately; an overall mean alone is not reported)

| Stratum | Minimum queries (Track R) | Notes |
| --- | --- | --- |
| Keyword (identifiers, part numbers, codes) | 50 | where BM25 is expected to lead |
| Paraphrase (no shared content words) | 50 | where dense retrieval is expected to lead |
| English | 50 | |
| Chinese (Traditional and Simplified) | 50 | Requires a multilingual pin first. `bge-small-en` is English-only, and the BM25 tokenizer treats a run of CJK characters as one token (a segmenter is needed). Until both exist, the Chinese results are **NOT_VALIDATED**, not zero. |
| Conflicting evidence (two documents or sections disagree) | 20 cases | Expected outcome: CONFLICTED → REVIEW_REQUIRED, never a silent choice |
| Missing evidence | 20 cases | Expected outcome: ABSTAINED sections, never filled |
| Scanned documents (image-only PDF) | 20 documents | Real scans: skew, noise, stamps, tables, mixed languages. OCR quality (character error rate) is reported apart from retrieval quality. |
| Cross-project or unauthorized | 10 probes | Expected outcome: refusal; any leak fails the run |

## Metrics

- **Retrieval:** Recall@k and Success@k (k = 1, 3, 5, 10), MRR@10, nDCG@5 and nDCG@10. Bootstrap 95%
  confidence intervals, plus a paired comparison per method pair (a randomization test). A difference whose
  interval crosses zero is "no measured difference".
- **Reranking:** report, per stratum, the incremental change over the method it reranks, and the share of queries
  it helped, hurt or left unchanged.
- **Grounding:**
  - citation correctness: the cited passage states the claim, checked independently of the evidence pack;
  - abstention precision and recall;
  - conflict detection rate;
  - rate of unsupported claims promoted, which must be 0.
- **OCR:** character error rate and word error rate against transcriptions, plus mean confidence against
  accuracy.
- **Cost:** per-stage receipt latency (median and p95) and model load time, per platform.

## Procedure

1. Freeze the code SHA, the model pins and the configuration.
2. Run the benchmark through the governed path (`scripts/benchmarks/document_intelligence_benchmark.py` for track S;
   a track-R runner reads judgments from the owner's store).
3. Store the outputs outside the repository with their SHA-256. Commit only aggregates.
4. A default changes only by owner decision, citing a track-R report in which the change is better on every
   stratum it affects and no worse elsewhere (within the confidence intervals).

## Windows stage-overhead profile (DI-9, measured 2026-11-14)

The profile covered seven governed BM25 → evidence tasks over 66 passages, with cProfile on Windows.

- Before: 4.72 s in total, of which **86% (4.08 s) was `BufferedReader.read`** under `ObjectStoreArtifacts.get`.
  That was 84 reads at about 49 ms each, because `read(MAX_READ_BYTES + 1)` allocates the full 256 MiB buffer for
  every read on Windows (64 ms per read in isolation, against 0.06 ms for a size-bounded read).
- Not the cause: artifact storage (`put` with fsync and replace, 0.11 s), verification and replay logic, hashing,
  JSON and pydantic (each under 0.1 s), and process startup (none in these stages).
- After the fix (`ccc14e8`, Core file, semantics unchanged): 0.34 s in total, 14× faster. Median BM25 stage 238 →
  19 ms; evidence stage 434 → 21 ms, now in line with Linux (11–13 ms).
- The dense stage is still dominated by re-embedding every passage per query (DI-3).
