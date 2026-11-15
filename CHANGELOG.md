# CHANGELOG

## Unreleased

### 2026-11-14 release baseline (local-supervised dogfood; production NOT_READY)

- PR #26: the PR #20 to #25 stack lands on `main` as merge commit `1fd5608` (tree identical to the validated RC `202df5c`). Owner-authorized; human code review of the cumulative diff not performed.
- PR #27: result-acceptance step-up guard and other existence probes resolve on the `search_path` (N-1).
- PR #28: vite 7.3.7, source-map-js 1.2.2 (dev toolchain).
- PR #29: `pados init` writes `.env` and the control token owner-only from creation (atomic replace).
- PR #30: certifi 2026.10.23 (critical emSign advisory), Mako 1.4.3.
- PR #31: security register for CodeQL and Dependabot alerts.
- PR #32: pypdf 6.21.4; untrusted PDFs parsed in a bounded isolated worker (P2-11).
- PR #33: OS-enforced isolation for the opt-in model worker (Windows Job Object; Linux RLIMIT_AS and seccomp). Still disabled by default.
- Release baseline, residual register and owner decisions: `docs/releases/PADOS_LOCAL_DOGFOOD_BASELINE_2026-11-14.md`.
- Final engineering report: `docs/releases/PADOS_FINAL_ENGINEERING_REPORT_2026-11.md`.

- Document Intelligence packs (PR #24, stacked on #22). Pinned local embedding (bge-small-en-v1.5) and
  cross-encoder (ms-marco-MiniLM-L-6-v2) engines via the optional `document-intelligence` extra. Governed
  `PASSAGE_RETRIEVAL_DENSE`, `PASSAGE_RANK_FUSION`, `PASSAGE_RERANK_CROSS_ENCODER` and `PASSAGE_EVIDENCE_PACK`
  stages, with templates from PDF or `ocr.document/v1` to evidence, and from evidence to a grounded
  ResultCandidate. Added an operator model-fetch script, a synthetic benchmark (`BENCHMARK_2026-11-14.md`) and
  residuals DI-1 to DI-10.

- Imported governing specification.
- Added P0 application skeleton, governance docs, Docker Compose, scripts, migrations, tests, and validation evidence.

