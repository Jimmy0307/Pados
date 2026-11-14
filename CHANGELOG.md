# CHANGELOG

## Unreleased

- Document Intelligence packs (PR #24, stacked on #22). Pinned local embedding (bge-small-en-v1.5) and
  cross-encoder (ms-marco-MiniLM-L-6-v2) engines via the optional `document-intelligence` extra. Governed
  `PASSAGE_RETRIEVAL_DENSE`, `PASSAGE_RANK_FUSION`, `PASSAGE_RERANK_CROSS_ENCODER` and `PASSAGE_EVIDENCE_PACK`
  stages, with templates from PDF or `ocr.document/v1` to evidence, and from evidence to a grounded
  ResultCandidate. Added an operator model-fetch script, a synthetic benchmark (`BENCHMARK_2026-11-14.md`) and
  residuals DI-1 to DI-10.

- Imported governing specification.
- Added P0 application skeleton, governance docs, Docker Compose, scripts, migrations, tests, and validation evidence.

