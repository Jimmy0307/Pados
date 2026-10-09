# Contract: `pados.execution.receipts`

## Purpose

Execution evidence. It holds no database or semantic mutation API; acceptance happens only through validation and CAS elsewhere.

## API

- `artifact_handoff(artifacts) -> list[dict]`
- `execution_receipt(*, run_id, attempt, contract, artifacts, provider_id, evidence_kind, ok) -> dict`
- `verify_execution_receipt(receipt | None, *, run_id, attempt, contract, artifacts) -> bool`
- `verify_artifact_bytes(artifact, content: bytes) -> bool`
- `verify_handoff_files(root, artifacts, expected_paths) -> bool`

## `artifact_handoff` (raises `ValueError(code)`)

- **Count.** At most 8 artifacts, else `ARTIFACT_BUDGET_EXCEEDED`.
- **Path.** Each `path` must be a non-empty `str`, relative (no leading `/` or `\`), contain no `\` and no `:`, have no `..` segment, and be unique, else `INVALID_ARTIFACT_PATH`.
- **Identity.** `sha256` must be exactly 64 lowercase hex characters, and `size_bytes` an `int` (not `bool`) ≥ 0, else `INVALID_ARTIFACT_IDENTITY`.
- **Credentials.** A credential-shaped path (`redact(path) != path`) gives `SECRET_IN_ARTIFACT_METADATA`.
- **Output.** `[{"path", "sha256", "size_bytes"}]` (exactly these keys), sorted by `path`.

## `execution_receipt` (persisted format; must be byte-compatible)

- **Validation.** `evidence_kind` must be one of `DETERMINISTIC_HOST`, `SIMULATION`, `LIVE_PROVIDER`, else `ValueError("EXECUTION_EVIDENCE_REQUIRED")`. `run_id` and `provider_id` must be non-empty and `1 <= attempt <= 3`, else `ValueError("INVALID_EXECUTION_BINDING")`.
- **Payload.** Exactly these keys:

  | Key | Value |
  |---|---|
  | `schema_version` | `"1"` |
  | `run_id`, `attempt` | as given |
  | `work_item_id` | `contract["work_contract_id"]` |
  | `contract_hash` | `canonical_hash(contract)` |
  | `provider_id`, `evidence_kind`, `ok` | as given |
  | `artifacts` | `artifact_handoff(artifacts)` |
  | `semantic_authority` | `False` |

- **Receipt.** The payload plus `receipt_id = "execution-sha256:" + canonical_hash(payload)` and `content_hash = canonical_hash(payload)`.

## `verify_execution_receipt`

- **True only if** the receipt is not `None`, the recomputed receipt (same run, attempt, contract and artifacts, using the receipt's own `provider_id`, `evidence_kind` and `ok`) is **exactly equal** to the given receipt, `ok is True`, and `evidence_kind != "SIMULATION"`.
- **Errors.** Any `KeyError`, `TypeError` or `ValueError` yields `False`.
- **Meaning.** A hash is integrity evidence only, never caller authentication.

## Helpers

- **`verify_artifact_bytes`.** True iff both `sha256` and `size_bytes` match the content.
- **`verify_handoff_files`.** Resolve `root` strictly, normalise the artifacts through `artifact_handoff`, and require the set of artifact paths to equal `expected_paths`. Each file must be ≤ 400,000 bytes; it is read through SafeWorkspaceFS bounded to its declared size and must match. Any error returns `False`.

**Tests:** `tests/consolidation/test_boundaries.py`, `tests/security/test_p6_21m_product_auth.py`, `tests/unit/test_consolidation_product_loop.py`, `tests/unit/test_p6_21g_product_loop.py`, `tests/unit/test_pados_codex_provider.py`, `tests/compute_node/*`, `tests/enterprise/test_enterprise_secretref.py`.
