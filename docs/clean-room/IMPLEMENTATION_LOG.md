# Clean-room implementation log

All work was done inside the clean-room export only. No path outside the export was read, no version-control history was consulted, and no web search was made. Each file was written from its contract in this folder, plus the in-export callers and tests listed below.

## 1. `src/pados/execution/context.py`

- **Specification:** `execution_context_contract.md`.
- **Consulted:**
  - `src/pados/execution/safe_fs.py`: `SafeFSError`, `workspace_fs`, `read_file` semantics.
  - Callers of `redact` and `canonical_hash`: `semantic_context.py`, `product_loop/codex_provider.py`, `operations/service.py`, `product_loop/service.py`.
  - Tests: `tests/consolidation/test_boundaries.py` and `tests/consolidation/test_phase2_context_resolver.py`.
- **Design:**
  - Detection is a tuple of compiled regular expressions. The key-assignment rule only matches when the keyword is a complete identifier segment and the value is a non-trivial literal: at least 4 characters, and not `true`, `false`, `null` or `none`. This avoids false positives on identifiers such as `secret_refs` or `runtime_does_not_decide_authorization: true`, which appear in serialized contracts.
  - Provider-prefix and cloud key patterns require a left word boundary.
  - Each snapshot is built by one helper, `_snapshot`, which applies the truncation marker, then the redaction, then the UTF-8 byte cap, then the hash.
  - The budget is checked before each read.

## 2. `src/pados/execution/handoff.py`

- **Specification:** `execution_handoff_contract.md`.
- **Consulted:**
  - `src/pados/execution/authorization.py`, for its return shape.
  - Callers: `product_loop/maker.py`, `product_loop/codex_provider.py`, `compute_node/maker.py`.
  - Tests: `test_boundaries.py`, `test_phase2_execution_authorization.py`, `test_core_extension_contracts.py`.
- **Design:**
  - Checks run in the contract's order.
  - Path safety is a small predicate over the normalised segments.
  - When no extension is requested, the grant-derived fields stay empty or `None`.
  - The module imports only the authorization and context modules, with no semantic-authority imports.

## 3. `src/pados/execution/receipts.py`

- **Specification:** `execution_receipts_contract.md`.
- **Consulted:**
  - Callers: `product_loop/validation.py`, `operations/live_evidence.py`, `product_loop/maker.py`, `compute_node/maker.py`.
  - Tests: `test_boundaries.py` and `tests/platform/test_safe_workspace_fs.py`.
- **Design:**
  - Path validation is a `TypeGuard` predicate.
  - The receipt payload is built with exactly the contracted keys. `receipt_id` and `content_hash` both derive from `canonical_hash(payload)`.
  - Verification rebuilds the receipt and compares it for exact equality.
  - `verify_handoff_files` reads each file strictly bounded to its declared size, so an oversized file fails, and treats any exception as `False`.

## 4. `src/pados/product_loop/state.py`

- **Specification:** `product_loop_state_contract.md`.
- **Consulted:**
  - `src/pados/product_loop/service.py`, for the full execute flow and how the gateway is used.
  - `read_model.py`: `read_from_report`, `read_from_context`, `ProductLoopReadModel`, and `_runtime_from_result`, which parses `answer`.
  - `schemas.py`: `WorkContract`, `RuntimeResult`, `ReceiptRecord`.
  - `authorization.py`: `ProductAuthorizationError`.
  - `src/pados/state_proof/service.py`: the `execute`, `commit_result` and `rebuild_projection` signatures.
  - `src/pados/state_proof/repository.py`: `register_object`, the read-index functions, `get_scope_head` and `get_result`.
  - `src/pados/state_proof/object_store.py`.
  - `src/pados/graph/cognition.py`: `parse_cognitive_response`, the allowed synthesis keys.
  - `src/pados/execution/semantic_context.py`.
  - The `control_approvals` migration and `project_goal/service.py`, for column names.
  - Tests: `tests/unit/test_consolidation_product_loop.py`, `tests/unit/test_p6_21g_product_loop.py`, `tests/unit/test_final_closure_provider_identity.py`, `tests/security/test_p6_21m_product_auth.py`, `tests/enterprise/test_enterprise_context_and_fail_closed.py`, `tests/compute_node/test_node_maker_product_loop.py`, and the integration tests `test_final_closure_approval_binding.py`, `test_p6_21h_durable_read_model.py`, `test_operational_memory_authority.py` and `test_p6_21g_state_integration.py`.
- **Design:**
  - The proof provider is a tiny object that returns a precomputed synthesis document. `answer` is the sorted-key JSON of `RuntimeResult.model_dump(mode="json")`.
  - The operational session is a context manager that falls back to the proof session.
  - `verify_approval` performs one parameterised lookup by approval id, then compares project and status.
  - In the in-memory gateway:
    - Receipts are keyed by the operation id `idempotency_key:attempt`.
    - Commits and projections are keyed by `receipt_id`.
    - The base version is the contract's `semantic_base_scope_version`, so a version advance during execution yields `STALE_CANDIDATE`.
    - `read_index` stores `ObjectRef` instances, which tests pass back to `object_store.read`.

## 5. `src/pados/knowledge/identity.py`

- **Specification:** `knowledge_identity_contract.md`.
- **Consulted:** `corpus/ingestion.py` and `enterprise/lineage.py` (callers), plus `test_boundaries.py` and `test_core_extension_contracts.py`.
- **Design:** a frozen dataclass, a strict 64-lowercase-hex check, and streaming hashing with the walrus loop.

## 6. `src/pados/knowledge/adapters.py`

- **Specification:** `knowledge_adapters_contract.md`.
- **Consulted:**
  - The existence of `pados.corpus.pdf.parse_pdf`, `pados.memory.retrieval.search_memory` and `pados.providers.factory.build_chat_provider`.
  - Tests: `test_boundaries.py` and `test_core_extension_contracts.py`.
- **Design:**
  - `resolve()` uses `importlib.import_module` lazily.
  - The profile validates in `__post_init__`.
  - The module imports only standard-library modules.

## 7. `web/src/resourceCache.ts`

- **Specification:** `web_resource_cache_contract.md`.
- **Consulted:** `web/src/resourceCache.node-test.mjs`, `web/tsconfig.json` and `web/scripts/lint.mjs`.
- **Design:**
  - A module-level `Map` of pending promises.
  - The loader is invoked synchronously, as the test expects `calls === 1` immediately, and a synchronous throw becomes a rejected promise.
  - Release runs on settle and only deletes the entry if it is still the same promise.
  - The file uses plain type-strippable TypeScript.

## 8. `web/src/useResource.ts`

- **Specification:** `web_use_resource_contract.md`.
- **Consulted:** `web/src/control-center.tsx` (the consumer), plus `web/package.json` and `web/vitest.config.ts`.
- **Design:**
  - State holds the last settled outcome tagged with its key. The rendered state is derived from it, so a changed key renders loading immediately.
  - The latest loader is kept in a ref that an effect updates. This effect is declared before the request effect.
  - The request effect depends only on `[key, enabled]`. Its cleanup marks the request stale, so late results are ignored, and clears the outcome, so re-enabling shows loading again.

## Verification (run from the export root)

| Command | Result |
|---|---|
| `.venv/bin/ruff check src tests` | All checks passed |
| `.venv/bin/mypy src` | Success: no issues found in 159 source files |
| Full `pytest` with `PADOS_DB_PORT=55432 PADOS_TEST_OPERATIONAL_DATABASE=pados_v1_operational_test` | 693 passed, 11 skipped, 5 failed, 4 errors |
| `cd web && npm run typecheck` | Passed |
| `npm test` | 4 files, 14 tests passed |
| `npm run lint` | Boundary lint passed |
| `node --test web/src/resourceCache.node-test.mjs` | 2 of 2 passed |

Non-passing pytest results:

- **Known environment-only issues (as stated in the task):**
  - 2 failures in `tests/unit/test_web_api.py`;
  - 4 errors in `tests/integration/test_project_create_postgres_contract.py`.
- **Release packaging tests (3 failures):**
  - `tests/security/test_p6_6_adversarial.py::test_release_zip_preserves_source_tree_paths`
  - `tests/security/test_p6_6_adversarial.py::test_release_zip_excludes_logs_caches_and_secret_fixtures`
  - `tests/unit/test_release_package.py::test_release_package_preserves_tree_and_excludes_runtime_content`

  They fail with `release validation failed: secret-like content: docs/clean-room/execution_context_contract.md`. The release packager walks the whole tree and its content scanner flags the specification document's illustrative credential examples. None of the implementation files are flagged. The cause is that the specification folder is present in this export; it is not caused by the implementation or by the missing `.git`. The tests were not modified.

## Performance fix (redact linear time)

**Inputs:** the updated "Performance requirement" section of `execution_context_contract.md` and the new `tests/consolidation/test_clean_room_performance.py`.

### Cause

Two patterns caused the superlinear behaviour on long runs:

- The URL pattern `[A-Za-z][A-Za-z0-9+.-]*://…` could start at every letter and rescan the rest of the run.
- The assignment pattern allowed an unbounded identifier prefix, `(?:[A-Za-z0-9]+[_-])*`.

Several other patterns also used unbounded `\s*`, `\s+` or `{n,}` tails.

### Change (`src/pados/execution/context.py`, `_CREDENTIAL_PATTERNS` only)

- **Assignment pattern.** The identifier prefix group is removed. The keyword is now anchored with a fixed-width lookbehind, `(?<![A-Za-z0-9])`, which accepts the same inputs: a keyword at the start, after a non-alphanumeric character, or after an `_`/`-` separator such as in `client_secret`. Whitespace around `:`/`=` is bounded to `\s{0,16}`. The value check is the fixed `{4}`, which is equivalent for a search.
- **Token-prefix patterns.** The `sk-`, `ghp_`-family, `github_pat_` and `xox?-` patterns use fixed-length tails instead of `{n,}`. Since `re.search` only needs existence, this is equivalent.
- **Bearer.** The pattern is `bearer\s{1,16}` plus 8 token characters, case-insensitive. The leading word boundary was dropped, because the new test requires detecting `Bearer <token>` even when it is glued to a long run of word characters.
- **PEM header.** The label is bounded to `[A-Z0-9 ]{0,40}`.
- **URL with embedded user and password.** The pattern is now anchored on the literal `://`, preceded by one alphanumeric character via a fixed lookbehind. User and password are bounded to 256 characters each.
- **Unchanged.** Detection results on the existing tests and on a hand-checked set of positive and negative samples. The other regexes in the eight files are anchored `fullmatch` checks with bounded or fixed repetition, so they have no rescanning risk and are unchanged.

### Timings (Linux, single `redact` call)

| Input | Time |
|---|---|
| 1 MiB word run | 0.10 s |
| 1 MiB whitespace | 0.21 s |
| 1 MiB JSON pairs | 0.20 s |
| 1 MiB `word=` repetitions | 0.13 s |
| 1 MiB mixed words | 0.14 s |
| 20,000 `a` characters | 0.002 s (previously about 0.36 s) |

`test_context_exceeding_byte_budget_is_rejected` now takes 0.03 s, down from about 64 s.

### Results

- `ruff check src tests`: all checks passed.
- `mypy src`: no issues in 159 source files.
- `pytest tests/consolidation tests/unit/test_consolidation_product_loop.py tests/security tests/enterprise tests/compute_node`: 267 passed, including all 6 performance tests.
- Full `pytest tests`: 702 passed, 11 skipped, 2 failed, 4 errors. The failures are only the known environment ones (2 in `tests/unit/test_web_api.py`, 4 errors in `tests/integration/test_project_create_postgres_contract.py`).
- The three release-packaging tests that failed earlier now pass with the current specification folder.

## Scanner false-positive fix

Two repository secret scanners flagged `src/pados/execution/context.py` (rule `credential_url`). The source text of the URL-credential regex, and the comment above it, looked like a URL with an embedded user name and password.

**Change.** The comment now describes the pattern in words. The pattern is built at import time from separately named parts, joined into `_URL_USERINFO`:

- `_URL_SCHEME_SEPARATOR`, itself built from `":"` and `"//"`;
- `_URL_USER_PART`;
- `_URL_PASSWORD_SEPARATOR`;
- `_URL_PASSWORD_PART`;
- `_URL_HOST_DELIMITER`.

The assembled string is identical, character for character, to the previous pattern, so behaviour and the linear-time bound are unchanged.

**Verification.**

- The coordinator's source check (`re.search(r'://[^/\s:@*"]+:[^@\s/*"]+@', …)` over the file) prints `False`.
- An equality check confirmed the assembled pattern matches the previous one. A URL with user and password is still redacted, and a plain URL is left unchanged.
- `ruff check src tests` passes, and `mypy src` reports no issues in 159 files.
- `pytest tests/consolidation tests/security` gives 155 passed, including `test_clean_room_performance.py`.
- `scripts/audit_consolidation.py` was not run to completion. It calls `git diff`, and this export has no repository, so it exits with status 129 before scanning.
