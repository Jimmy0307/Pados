# Contract: `pados.execution.context`

## Purpose

Build a **bounded, sanitized, content-hashed snapshot** of a project's instruction sources for a governed work package. Context is *data*, never permission.

## Public API (names and signatures are compatibility requirements)

- `redact(text: str) -> str`
- `canonical_hash(value: Any) -> str`
- `@dataclass(frozen=True) class InstructionSource` with fields: `path: str`, `kind: str`, `text: str`, `content_hash: str`, `bytes_read: int`, `truncated: bool`, `redacted: bool`.
- `@dataclass(frozen=True) class ProjectContext` with fields: `sources: tuple[InstructionSource, ...]`, `scope_id: str`, `scope_version: int`, `warnings: tuple[str, ...]`, `instructions_are_data_not_permission: bool = True`.
  - `to_dict() -> dict`: plain nested dict/list of exactly these fields.
  - `fingerprint` (property) `-> str`: `canonical_hash(self.to_dict())`.
- `class ProjectContextLoader(workspace: Path, *, max_source_bytes: int = 16_000, max_total_bytes: int = 64_000)`
- `ProjectContextLoader.load(*, scope_id: str, semantic: Mapping[str, Any]) -> ProjectContext`
- Module constants:
  - `DEFAULT_SOURCES`, the repository instruction files considered, in this order: `PADOS_PROJECT.md`, `README.md`, `AGENTS.md`, `CLAUDE.md`, `ROADMAP.md`, `BACKLOG.md`, `DEVLOG.md`, `DECISIONS.md`.
  - `SEMANTIC_FIELDS`, the semantic keys considered, in this order: `active_goal`, `open_blockers`, `pending_decisions`, `recent_accepted_results`, `promoted_memory`, `evidence`, `scope_version`.

## Serialization and hashing (persisted format; must be exact)

`canonical_hash(value)` is the lowercase hex SHA-256 of the UTF-8 bytes of `value` serialized as JSON with:

- sorted keys;
- ASCII escaping of non-ASCII characters;
- separators `","` and `":"` (no whitespace).

Other modules (receipts, handoff) depend on this exact rule.

## `redact(text)`

- **Detection.** If `text` contains credential-shaped material, return exactly `[REDACTED: credential-shaped source]`; otherwise return `text` unchanged. The whole source is dropped, not partially masked. Credential-shaped material includes:
  - assignments of a field whose name is an API key, secret, token, password or authorization value, case-insensitive;
  - a bearer-style authorization prefix followed by a value;
  - common provider token prefixes (OpenAI-style, GitHub personal/OAuth tokens, Slack tokens);
  - AWS access key ids;
  - PEM private-key block headers;
  - URLs with embedded credentials (user and password before the host).
- **Plain paths.** Must return an ordinary relative path unchanged. Callers use `redact(x) != x` as a "credential-shaped" test.

## Loader rules

1. **Constructor.** Resolve `workspace` strictly (it must exist) and require a directory. Both budgets must be ≥ 64. On violation raise `ValueError("INVALID_CONTEXT_BUDGET_OR_ROOT")`.
2. **`scope_id`.** Must match: first character alphanumeric, then up to 159 more characters from `[A-Za-z0-9_.:-]`. Otherwise raise `ValueError("INVALID_CONTEXT_SCOPE_ID")`.
3. **Scope version.** `semantic["scope_version"]` must be an `int` (not `bool`) and ≥ 0. Otherwise raise `ValueError("AUTHORITATIVE_SCOPE_VERSION_REQUIRED")`. This value becomes `ProjectContext.scope_version`.
4. **Budget.** A running remaining budget starts at `max_total_bytes`. Each source's limit is `min(remaining, max_source_bytes)`. When a limit is below 64, the source is skipped with warning `…:budget_exhausted`.
5. **Semantic sources come first**, in `SEMANTIC_FIELDS` order, so repository text cannot crowd them out.
   - A key absent from `semantic` gives warning `semantic:<name>:unavailable`.
   - Otherwise the value is serialized deterministically (sorted keys, ASCII) and added with path `pados://<scope_id>/<name>` and kind `semantic`. A budget skip gives warning `semantic:<name>:budget_exhausted`.
6. **Repository sources**, in `DEFAULT_SOURCES` order, are read through SafeWorkspaceFS (`pados.execution.safe_fs.workspace_fs(root).read_file(name, max_bytes=limit, allow_truncate=True)`). Symlinks, reparse points and non-regular files are never followed.
   - A missing file (`None`) is silently skipped.
   - `SafeFSError` with code `SAFE_FS_NOT_REGULAR_FILE` gives warning `<name>:not_regular`.
   - Any other `SafeFSError` or `OSError` gives warning `<name>:unreadable_or_symlink`.
   - A skip for budget gives warning `<name>:budget_exhausted`.
   - Kind is `repository`, path is the file name.
7. **Per-source snapshot.** Decode UTF-8, dropping invalid bytes.
   - If the raw source exceeds the limit, the text becomes exactly `[TRUNCATED: source omitted]` and `truncated=True`.
   - Otherwise the text is `redact(decoded)`; `redacted` is True iff the text differs from the decoded content.
   - The stored text is capped to the limit in UTF-8 bytes.
   - `content_hash` is the SHA-256 hex of the stored text's UTF-8 bytes (a snapshot fingerprint, not a file identity). `bytes_read` is the stored text's UTF-8 byte length, and it is subtracted from the remaining budget.
8. **Result.** `ProjectContext(tuple(sources), scope_id, scope_version, tuple(warnings))`.

## Security invariants

No authorization is derived from context. Credential-shaped sources never appear verbatim. Reads are bounded and no-follow.

## Behaviour-defining tests

`tests/consolidation/test_boundaries.py`, `tests/consolidation/test_phase2_context_resolver.py`, `tests/unit/test_consolidation_product_loop.py`, `tests/unit/test_context_budget.py`, `tests/platform/test_safe_workspace_fs.py`, `tests/security/*`.

## Performance requirement (added after behavioural verification)

- **Linear time.** `redact` must run in time linear in the input length, with no catastrophic or quadratic backtracking. Callers apply it to serialized semantic context of up to several hundred KiB, and repeatedly (for every string in a nested structure).
- **Measurable bound.** For inputs of 1 MiB (long runs of word characters, long runs of whitespace, long JSON-like `"key": "value", …` sequences, and long `word=` repetitions), a single call must complete in well under 1 second on ordinary hardware.
- **Detection unchanged.** Credential-shaped content must still be detected inside large inputs.
- **Defining test:** `tests/consolidation/test_clean_room_performance.py`.
