# Contract: `pados.knowledge.adapters`

## Purpose

A declared map of knowledge-platform adapter boundaries onto existing PADOS implementations. Unavailable lanes have **no pretend implementation**. Knowledge derivations never promote memory or accept semantic results; the module imports no semantic-authority code, as checked by `test_core_extension_contracts.py`.

## API

- **`@dataclass(frozen=True) class AdapterBoundary(name: str, module: str | None, symbol: str | None, status: str)`.**
  - `resolve()` imports `module` and returns attribute `symbol`.
  - When `module` or `symbol` is `None` it raises `RuntimeError(f"{name.upper()}_UNAVAILABLE")`.
- **`ADAPTER_BOUNDARIES`.** A tuple with unique names and statuses from {`IMPLEMENTED`, `BLOCKED`, `UNVERIFIED`}:

  | Name | Module / symbol | Status |
  |---|---|---|
  | `DocumentParserAdapter` | `pados.corpus.pdf` / `parse_pdf` | `IMPLEMENTED` |
  | `OCRAdapter` | none | `BLOCKED` |
  | `RetrievalAdapter` | `pados.memory.retrieval` / `search_memory` | `IMPLEMENTED` |
  | `EmbeddingAdapter` | none | `BLOCKED` |
  | `RerankerAdapter` | none | `BLOCKED` |
  | `GenerationAdapter` | `pados.providers.factory` / `build_chat_provider` | `UNVERIFIED` |

- **`@dataclass(frozen=True) class KnowledgeLibraryProfile`.** Fields:

  | Field | Default |
  |---|---|
  | `library_id: str` | — |
  | `project_id: str` | — |
  | `max_source_bytes: int` | `10_000_000` |
  | `private_evidence_may_leave_process: bool` | `False` |
  | `training_collection_enabled: bool` | `False` |
  | `citation_required: bool` | `True` |

  Validation on construction:
  - `library_id` must match `[a-zA-Z0-9_-]{1,128}`, else `ValueError("INVALID_LIBRARY_ID")`.
  - `project_id` must be non-empty and `1 <= max_source_bytes <= 100_000_000`, else `ValueError("INVALID_LIBRARY_SCOPE_OR_BUDGET")`.
  - Either external evidence egress or training collection enabled raises `PermissionError("EXTERNAL_OR_TRAINING_AUTHORIZATION_NOT_IMPLEMENTED")` (fail closed).
- **`assert_source_scope(*, project_id: str, byte_size: int)`.** A different project gives `PermissionError("KNOWLEDGE_PROJECT_SCOPE_MISMATCH")`. A byte size outside `0 … max_source_bytes` gives `ValueError("KNOWLEDGE_SOURCE_BUDGET_EXCEEDED")`.

**Tests:** `tests/consolidation/test_boundaries.py`, `tests/consolidation/test_core_extension_contracts.py`.
