# Enterprise source lineage

## Identity

`EnterpriseSource.identity` is the existing PADOS `SourceIdentity` (`sha256:<content hash>`, byte size). The location is lineage, never identity: the same bytes at another location have the same `source_id` (tested).

Each source preserves:

- `source_id`, `content_hash`, `byte_size`
- `source_type`, `location_ref`, `ingested_at`
- `classification`, `tenant_id`, `project_id`
- `parser_version` (`<extractor_id>:<extractor_version>`), `chunking_version`, `embedding_version`
- `provenance` chain

Source ACL facts are carried on the object but are not emitted in `lineage()`.

## Derived objects

Derived ids are deterministic (`derived_id(kind, parent, …)`). `LineageGraph.add` enforces typed parents; a derived object without lineage is rejected (`DERIVED_OBJECT_REQUIRES_LINEAGE`).

| Kind | Allowed parents |
|---|---|
| SOURCE | none |
| NORMALIZED_ARTIFACT | SOURCE |
| CHUNK | NORMALIZED_ARTIFACT |
| EMBEDDING | CHUNK |
| RETRIEVAL_PROJECTION | CHUNK, EMBEDDING |
| GRAPH_PROJECTION | CHUNK, NORMALIZED_ARTIFACT |
| CACHE | RETRIEVAL_PROJECTION, CHUNK |
| EVALUATION_ARTIFACT | CHUNK, RETRIEVAL_PROJECTION |
| DATASET_REFERENCE | CHUNK, EVALUATION_ARTIFACT |
| TRAINED_ARTIFACT | DATASET_REFERENCE |

No retrieval result can enter context without its source identity: `presentation_filter` withholds `SOURCE_IDENTITY_MISSING`, and every `EvidenceItem` carries full provenance.

## Delete / revoke

- **`plan_revocation(source_id, reason)` is a deterministic dry run.** It returns:
  - affected nodes by kind, `affected_count`;
  - `invalidate_automatically`;
  - `human_review_required`, `human_review_items` (DATASET_REFERENCE, TRAINED_ARTIFACT);
  - `model_weights_deleted: false`;
  - a stable `plan_hash`.

  Planning changes nothing (tested).
- **`apply_revocation(plan)` only masks.** It marks the source and its automatic projections revoked, so the coarse retrieval scope excludes them. Human-review items are never touched, and nothing is physically deleted.
- **Trained models are never deleted automatically.** Retraining or retirement is a human decision.
