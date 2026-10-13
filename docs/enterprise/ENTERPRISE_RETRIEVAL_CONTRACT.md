# Enterprise retrieval contract (`pados.enterprise.retrieval.v1`)

## Request (`RetrievalRequest`)

| Field | Meaning |
|---|---|
| `project_id`, `tenant_id` | PADOS project identity and tenant binding. Must equal the `AccessContext` |
| `principal_id`, `session_id` | User/session identity. Must equal the `AccessContext` |
| `query` | 1–2000 characters. Stored in the fingerprint only as a SHA-256 |
| `source_ids`, `source_types` | Optional source filters |
| `classification_ceiling` | Request ceiling. The effective ceiling is the lowest of this, the caller clearance and the capability maximum |
| `top_k` | 1–50 |
| `strategy` | `EXACT`, `VECTOR`, `HYBRID` |
| `budget` | `EvidenceBudget(max_sources, max_chunks, max_context_tokens, max_restricted_items)` |

## Pipeline

1. Coarse scope.
2. Exact/term seed, then optional graph expansion, then vector, all as separate backend protocols.
3. Reciprocal-rank fusion. Component scores are kept.
4. Presentation filter.
5. Rerank.
6. Budget.
7. Package.

## Response (`RetrievalResponse.to_dict()`)

| Field | Content |
|---|---|
| `source_identities` | `sha256:` ids of all evidence sources |
| `evidence[]` | `chunk_id`, `source_id`, `classification`, `span {page, char_start, char_end}`, `text` (omitted in content-free projections), `text_sha256`, `token_estimate`, `retrieval_scores {exact, vector, graph, fused}`, `rerank_score`, `provenance` (full source lineage plus extractor, chunking and embedding versions) |
| `authorization` | capability id, `ALLOW`, effective ceiling, grant source, coarse scope, `withheld` counts by reason |
| `backends` | backend ids and versions |
| `reranker` | the rerank report (below) |
| `budget` | limits, used, dropped by reason |
| `versions` | contract, chunking, embedding |
| `semantic_authority` / `promotable_to_truth` | always `NONE` / `false` |

`package_hash` is the SHA-256 of the response without timing fields, so identical requests over identical sources give identical packages.

## Reranker report

- `reranker_id`, `model`, `version`, `mode`
- `input_count`, `output_ranking`, `shadow_ranking` (SHADOW only), `applied`
- `latency_ms`, `resource_class`
- `provenance {scored_chunk_ids, prior_order}`, and `error` (SHADOW failures only)

## Backends

The protocols are `RetrievalBackend` and `GraphExpander`. V1 ships these in-process implementations:

- `pados.retrieval.exact.in-memory`
- `pados.retrieval.vector.in-memory` (PADOS `deterministic_embedding`, 32-dimensional)
- `pados.graph.adjacent-chunks` (non-authoritative neighbour expansion)

PostgreSQL FTS, pgvector, Elasticsearch, Milvus or a graph database can implement the same protocols later. None was added: V1 needs no new infrastructure, and a persistent PostgreSQL index would require a governed migration, which is out of scope for a frozen Core.
