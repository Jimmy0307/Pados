# RQ1 lexical retrieval experiment (EXPERIMENTAL — NOT RELEASE ACCEPTED)

Status: **EXPERIMENTAL. NOT RELEASE ACCEPTED. LEXICAL-ONLY BASELINE.**
This branch is evidence, not a release candidate. It must not be merged into the RC2 line, and its
retrieval result must not be read as passing the retrieval quality gate.

## Identity

| Item | Value |
| --- | --- |
| RC2 base | `229e6b77b04ee3639d727dce0988b77cea12af15` |
| RQ1 candidate | `e1d7444` (tree `85113859f23f855828d4a01fb7f94275071ae579`) |
| RQ1.1 certification commit | `197d97dfaadb8ca91028fc6070fe954ea45f0cce` (tree `3c8a625cf331d5824decf6cd32e66ee200b384a7`) |

## What the branch contains

- `src/pados/graph/retrieval.py`: pure lexical ranking (BM25 over title x3 + content, phrase bonus,
  one-hop graph-local expansion, MMR selection with IDF-weighted coverage). No embedding capability
  exists in the graph path; diagnostics state `semantic = UNAVAILABLE_NO_EMBEDDING_CAPABILITY`.
- Context assembly: ranked evidence segment; non-root IDEA and OUTPUT nodes are never evidence;
  `_fit_records` truncates instead of dropping records.
- Grounding: server-assigned evidence ids `E1..En` bound to source node, title and content hash; the
  prompt carries an EVIDENCE block; references outside the provided ids are rejected (`INVALID_SCHEMA`).
- Tests: `tests/unit/test_rq1_*.py`, including certification of IDEA exclusion and IDF coverage.

## Measured result (gold set Q1-Q5, question-only selection, tree `3c8a625c`)

| Question | Required sections retrieved |
| --- | --- |
| Q1 | 1/2 |
| Q2 | 1/2 |
| Q3 | 0/2 |
| Q4 | 1/2 |
| Q5 | 2/2 |
| **Total** | **5/10 — gate (10/10) NOT met** |

RC2 baseline on the same gold set: 1/10.

## Why the gate is not met

| Missing section | Rank | Primary reason |
| --- | --- | --- |
| Q1 README § Architecture | 59 | QUERY_VOCABULARY_GAP (no lexical or graph path) |
| Q2 SECURITY § Trust Boundaries | 15 | RANK_TOO_LOW (vocabulary gap underneath) |
| Q3 Resolver steps | 8 | RANK_TOO_LOW |
| Q3 Resolver steps (cont. 1) | 12 | RANK_TOO_LOW |
| Q4 6. Storage modes | 12 | RANK_TOO_LOW |

PADOS bundles no embedding or reranker engine (`packs/retrieval/engines.py` is contract-only and refuses
with `ENGINE_NOT_INSTALLED`). The deterministic hash embedding in `enterprise/retrieval.py` is not a
semantic model and must not be used as one. Closing the gap requires a governed semantic capability,
which is out of scope for this experiment.

## Open limitations

- Claim support is not validated: only reference validity (the cited id exists in this context) is enforced.
- Evidence binds to the graph node and its content hash, not to the original source artifact's SHA-256.
