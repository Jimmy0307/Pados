# Contract: `pados.knowledge.identity`

## Purpose

Physical source identity is the **content hash only**. Descriptive paths and locations are lineage, never identity.

## API

- `@dataclass(frozen=True) class SourceIdentity(content_hash: str, byte_size: int)` with property `source_id -> "sha256:" + content_hash`. `content_hash` is 64-character lowercase hex SHA-256.
- `source_identity_from_bytes(content: bytes) -> SourceIdentity`: the SHA-256 of all bytes, and the length.
- `bundle_source_id(member_hashes: Iterable[str]) -> str`:
  - **Inputs.** Each member must be 64 lowercase hex characters, and there must be at least one; otherwise `ValueError("BUNDLE_MEMBER_HASH_INVALID")`.
  - **Singleton.** A single member returns `"sha256:<hash>"`; a singleton is not a new bundle.
  - **Several members.** Returns `"bundle-sha256:" + sha256("\n".join(sorted(members)).encode("ascii")).hexdigest()`. This makes it order-independent. It is a persisted identity format.
- `physical_file_sha256(path: Path, *, chunk_size: int = 1 MiB) -> str`: the streaming SHA-256 hex of the file. `chunk_size < 1` raises `ValueError("HASH_CHUNK_SIZE_MUST_BE_POSITIVE")`.

**Tests:** `tests/consolidation/test_boundaries.py`, `tests/consolidation/test_core_extension_contracts.py`, `tests/enterprise/*` (source identity reuse), corpus ingestion tests.
