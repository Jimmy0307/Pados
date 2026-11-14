# Corpus Operability Contract

This is the operability contract record for component C08 (Corpus, ingestion and retrieval) in
`scripts/release/operability_contracts.v1.json`. It states what the component does, what it reads and writes, and
how it fails, as the code does today. Recovery steps are in `docs/operations/RECOVERY_RUNBOOKS.md` (section
"Corpus"). Paths are relative to `src/pados/`.

## Corpus operability contract

### Purpose and boundary

The corpus is a bounded store of research PDFs that the operator controls: stage, scan, review, authorize, ingest,
then keyword search with evidence spans.

Out of scope by design:
- no watcher
- no scheduler
- no cloud model
- no OCR
- no semantic retrieval (`corpus/runtime.py`, module docstring)

The batch authorization policy must keep the following false: `allow_ocr`, `allow_cloud_models`,
`allow_source_modification` and `allow_operational_migration`. `embedding_policy` must be `KEYWORD_ONLY`
(`corpus/configuration.py`).

Search is a case-insensitive substring match (`mode: "keyword"`). The parser reads only a PDF's text layer
(`TEXT_FIRST_PYPDF`).

### Interface

The routes are served by the core API on loopback only (`corpus/api.py`, `_local_only`):

| Route | Purpose |
|---|---|
| `GET /api/runtime` | Runtime provenance and revisions, credentials masked |
| `GET /api/projects/{id}/corpus/status` | Counts and project projection state |
| `POST /api/projects/{id}/corpus/batches` | Stage uploaded PDFs as a new batch (201) |
| `GET /api/projects/{id}/corpus/batches` | List batches (paginated) |
| `GET /api/corpus/batches/{batch_id}` | The batch manifest |
| `POST /api/corpus/batches/{batch_id}/scan`, `/review`, `/authorize`, `/ingest`, `/retry` | Lifecycle actions |
| `GET /api/projects/{id}/corpus/documents`, `GET /api/corpus/documents/{id}` (and `/versions`, `/pages/{n}`) | Read documents |
| `POST /api/projects/{id}/corpus/search`, `GET /api/corpus/evidence/{id}` | Keyword search and evidence spans |

The stable `pados` CLI has no corpus command; `pados doctor` checks the corpus migration lane. `corpus/runtime.py`
holds a separate milestone launcher (P6.15) that is not an installed entry point.

### State

**A batch** is a manifest file under the artifact root (`batches/`). Its states are:

```text
STAGED -> SCANNED -> PENDING_HUMAN_AUTHORIZATION -> AUTHORIZED -> INGESTING -> PUBLISHED or FAILED
```

- Scan may repeat from `STAGED`, `SCANNED` or `PENDING_HUMAN_AUTHORIZATION`.
- Ingest runs from `AUTHORIZED`, from `FAILED` (retry) or from `INGESTING`.
- Every other transition is refused (`FORBIDDEN_TRANSITION`).

**A document** moves through `IngestionState` (`corpus/ingestion.py`). It has an ordered path to `PUBLISHED` and
terminal failure states.

**Records** live in the dedicated corpus database:
- the source tables: `corpus_sources`, `corpus_ingestion_batches`, `corpus_documents`, `corpus_document_versions`,
  `corpus_pages`, `corpus_chunks`, `corpus_evidence_spans` and `corpus_ingestion_events` (`corpus/persistence.py`)
- a projection of the operational project

Authorization receipts are JSON files under the artifact root (`authorizations/`).

**The corpus lane** is `PADOS_CORPUS_DATABASE_NAME` (default `pados_corpus_runtime`).
- It accepts exactly the corpus revision `PADOS_CORPUS_REVISION` (default `20260730_0011`).
- The excluded revision `20260730_0012` is refused on every lane (`config.py`).
- The operational and corpus databases must be different.

### Dependencies

- **The operational database:** read in a `READ ONLY` transaction. It stays authoritative for the project, which is
  projected into the corpus database (created, unchanged or refreshed). PostgreSQL has no cross-database
  transaction, so the read closes before the corpus write opens (`corpus/project_context.py`).
- **The corpus database:** all document writes for one ingest commit in one session.
- **`pypdf`:** text layer only.
- **The staging root:** `PADOS_CORPUS_APPROVED_STAGING_ROOT` (default `<artifact root>/staging`). The artifact root
  defaults to `runtime/artifacts/p6_15_operator`.

### Authorization and limits

- **Receipts.** The backend writes them (`authorize_batch`), not the client.
  - The authorizer is `PADOS_CORPUS_OPERATOR_NAME`, or the OS user if that is unset.
  - Expiry is 24 h by default, allowed 1-168 h.
  - At ingest the receipt is verified again: hash, batch and policy scope, expiry, and the exact selected files.
- **Containment.**
  - Uploads must be simple `.pdf` names with MIME type `application/pdf`.
  - The staging tree is checked against links and against changes since review.
  - Child paths refuse traversal and symbolic links (`corpus/security.py`).
- **Limits.**
  - 100 documents per batch.
  - 50 MiB per file by default, configurable up to 500 MiB.
  - At most 100 search results.
  - A query of at most 200 characters.
  - A 3 s database timeout, configurable from 1 to 60 s.
  - Chunks of 1800 characters with a 250-character overlap.
  - Evidence quotes are cut to 600 characters.
- **Classification.** Only the project's classification is projected. Documents carry no classification of their
  own (R19-2).
- **Redaction.** Runtime provenance and errors mask passwords, tokens and connection strings.

### Idempotency, retry and failure

- **Retry** reconciles first:
  - a `PUBLISHED` batch answers `SAFE_RESUME_ALREADY_PUBLISHED`
  - a `FAILED` or `AUTHORIZED` batch ingests again
  - any other state is `FORBIDDEN_TRANSITION`
- **Mutations.** The corpus POST actions are `RECONCILE_BEFORE_RETRY` mutations (`operability/mutations.py`).
- **Duplicates.** An identical file already present yields `EXACT_DUPLICATE_EXISTING_VERSION` and inserts nothing.
- **Failed ingest.** It leaves the batch `FAILED` with `failed_at`.
- **Failed staging.** It removes the files it staged.
- **Changed sources.** A source file changed during ingest is reported as `SOURCE_MODIFIED`, with
  `write_occurred=true` because the check runs after the commit.

Error codes the operator service raises include `HUMAN_AUTHORIZATION_REQUIRED`, `UNAUTHORIZED_INGEST`,
`AUTHORIZATION_EXPIRED`, `AUTHORIZATION_MALFORMED`, `MANIFEST_HASH_MISMATCH`, `FILE_HASH_CHANGED`,
`STAGING_TREE_CHANGED`, `SYMLINK_REPARSE_ESCAPE`, `PATH_BOUNDARY_VIOLATION`, `FILE_SIZE_LIMIT_EXCEEDED`,
`DOCUMENT_CEILING_EXCEEDED`, `FORBIDDEN_TRANSITION`, `PROJECT_NOT_FOUND`, `OPERATIONAL_UNAVAILABLE`,
`CORPUS_UNAVAILABLE`, `INGEST_FAILED` and `QUERY_REQUIRED`. The API guard answers `LOCAL_ONLY`.

### Events

- **Ingest.** It writes one `corpus_ingestion_events` row per document state transition, with a deterministic id.
  When the `audit_events` table exists, it also writes one `CORPUS_INGESTION_PUBLISHED` row.
- **Scan, review and authorize.** They write only the manifest and receipt files, not database events.

### Health

`/health/ready` reports the lane as the non-required check `corpus_database` (`operability/health.py`):

| Result | Code |
|---|---|
| `OK` | The lane is usable |
| `NOT_CONFIGURED` | `CORPUS_DB_UNCONFIGURED` |
| `DEGRADED` | `CORPUS_MIGRATION_STALE` (reachable, not at the corpus revision) |
| `DEGRADED` | `CORPUS_DB_UNAVAILABLE` |

A corpus failure never blocks readiness. The core loop keeps working; only ingestion and retrieval stop.

### Known limits

- The document, page and evidence reads take a global id and do not re-check the operational project. Only the
  project-scoped reads do (status, batches, documents list, search). The API is loopback-only for the single local
  operator.
- `GET /api/corpus/documents/{id}/versions` returns the same record as the document read.
- The milestone launcher in `corpus/runtime.py` writes the fixed operational revision `20260711_0009` into its
  projection metadata, not the configured one. The web operator path uses the configured revision.
