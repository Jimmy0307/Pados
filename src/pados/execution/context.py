# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded project context snapshots for governed work.

The loader collects authoritative semantic fields and a fixed list of
repository instruction files into a size-limited, credential-scrubbed and
content-hashed snapshot. The snapshot is reference data for a worker; it
never carries or implies any permission.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pados.execution.safe_fs import SafeFSError, workspace_fs

DEFAULT_SOURCES: tuple[str, ...] = (
    "PADOS_PROJECT.md",
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "ROADMAP.md",
    "BACKLOG.md",
    "DEVLOG.md",
    "DECISIONS.md",
)

SEMANTIC_FIELDS: tuple[str, ...] = (
    "active_goal",
    "open_blockers",
    "pending_decisions",
    "recent_accepted_results",
    "promoted_memory",
    "evidence",
    "scope_version",
)

REDACTED_MARKER = "[REDACTED: credential-shaped source]"
TRUNCATED_MARKER = "[TRUNCATED: source omitted]"
_MIN_BUDGET = 64
_SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")

_CREDENTIAL_WORDS = (
    r"api[_-]?key|apikey|secret(?:[_-]?key)?|client[_-]?secret|access[_-]?key|private[_-]?key"
    r"|token|auth[_-]?token|access[_-]?token|refresh[_-]?token|password|passwd|pwd"
    r"|authorization"
)
# URL user-info detection, assembled from named parts: the scheme separator
# (anchored after one alphanumeric character), a bounded user part, the
# user/password separator, a bounded password part and the host delimiter.
_URL_SCHEME_SEPARATOR = r"(?<=[A-Za-z0-9])" + ":" + "//"
_URL_USER_PART = r"[^\s/:@]{1,256}"
_URL_PASSWORD_SEPARATOR = ":"
_URL_PASSWORD_PART = r"[^\s/@]{1,256}"
_URL_HOST_DELIMITER = "@"
_URL_USERINFO = (
    _URL_SCHEME_SEPARATOR
    + _URL_USER_PART
    + _URL_PASSWORD_SEPARATOR
    + _URL_PASSWORD_PART
    + _URL_HOST_DELIMITER
)
# Every pattern starts with a literal keyword, prefix or fixed-width
# lookbehind and uses only bounded repetition afterwards, so each search runs
# in time linear in the input length (no rescanning of long runs).
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    # name = value / "name": "value" style assignments of a non-trivial value.
    # The keyword may be the tail of an identifier joined by "_" or "-"
    # (e.g. client_secret), but not glued to a longer word.
    re.compile(
        r"(?<![A-Za-z0-9])(?:" + _CREDENTIAL_WORDS + r")(?![A-Za-z0-9_])"
        r"[\"']?\s{0,16}[:=]\s{0,16}[\"']?(?!(?:true|false|null|none)(?![A-Za-z0-9]))"
        r"[^\s\"',;{}\[\]()<>]{4}",
        re.IGNORECASE,
    ),
    # A bearer token is detected even when glued to preceding text.
    re.compile(r"bearer\s{1,16}[A-Za-z0-9._~+/=-]{8}", re.IGNORECASE),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8}"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{16}"),
    re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{16}"),
    re.compile(r"(?<![A-Za-z0-9])xox[a-z]-[A-Za-z0-9-]{8}"),
    re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
    re.compile(r"-----BEGIN[A-Z0-9 ]{0,40}PRIVATE KEY-----"),
    # A URL whose authority embeds a user name and a password; the scheme
    # itself is not needed to detect it.
    re.compile(_URL_USERINFO),
)


def redact(text: str) -> str:
    """Replace the whole text with a marker if it holds credential-shaped material."""
    if any(pattern.search(text) for pattern in _CREDENTIAL_PATTERNS):
        return REDACTED_MARKER
    return text


def canonical_hash(value: Any) -> str:
    """SHA-256 of the canonical JSON form (sorted keys, ASCII, compact separators)."""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InstructionSource:
    path: str
    kind: str
    text: str
    content_hash: str
    bytes_read: int
    truncated: bool
    redacted: bool


@dataclass(frozen=True)
class ProjectContext:
    sources: tuple[InstructionSource, ...]
    scope_id: str
    scope_version: int
    warnings: tuple[str, ...]
    instructions_are_data_not_permission: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "sources": [asdict(source) for source in self.sources],
            "scope_id": self.scope_id,
            "scope_version": self.scope_version,
            "warnings": list(self.warnings),
            "instructions_are_data_not_permission": self.instructions_are_data_not_permission,
        }

    @property
    def fingerprint(self) -> str:
        return canonical_hash(self.to_dict())


def _snapshot(path: str, kind: str, raw: bytes, limit: int) -> InstructionSource:
    decoded = raw.decode("utf-8", errors="ignore")
    if len(raw) > limit:
        text, truncated, redacted = TRUNCATED_MARKER, True, False
    else:
        text = redact(decoded)
        truncated, redacted = False, text != decoded
    encoded = text.encode("utf-8")
    if len(encoded) > limit:
        text = encoded[:limit].decode("utf-8", errors="ignore")
        encoded = text.encode("utf-8")
    return InstructionSource(
        path=path,
        kind=kind,
        text=text,
        content_hash=hashlib.sha256(encoded).hexdigest(),
        bytes_read=len(encoded),
        truncated=truncated,
        redacted=redacted,
    )


class ProjectContextLoader:
    """Collect a bounded context snapshot for one approved workspace."""

    def __init__(
        self, workspace: Path, *, max_source_bytes: int = 16_000, max_total_bytes: int = 64_000
    ) -> None:
        try:
            root = Path(workspace).resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError("INVALID_CONTEXT_BUDGET_OR_ROOT") from exc
        if (
            not root.is_dir()
            or max_source_bytes < _MIN_BUDGET
            or max_total_bytes < _MIN_BUDGET
        ):
            raise ValueError("INVALID_CONTEXT_BUDGET_OR_ROOT")
        self.workspace = root
        self.max_source_bytes = max_source_bytes
        self.max_total_bytes = max_total_bytes

    def load(self, *, scope_id: str, semantic: Mapping[str, Any]) -> ProjectContext:
        if not isinstance(scope_id, str) or not _SCOPE_ID.fullmatch(scope_id):
            raise ValueError("INVALID_CONTEXT_SCOPE_ID")
        scope_version = semantic.get("scope_version")
        if isinstance(scope_version, bool) or not isinstance(scope_version, int) or scope_version < 0:
            raise ValueError("AUTHORITATIVE_SCOPE_VERSION_REQUIRED")

        sources: list[InstructionSource] = []
        warnings: list[str] = []
        remaining = self.max_total_bytes

        # Semantic fields go first so repository text can never crowd them out.
        for name in SEMANTIC_FIELDS:
            if name not in semantic:
                warnings.append(f"semantic:{name}:unavailable")
                continue
            limit = min(remaining, self.max_source_bytes)
            if limit < _MIN_BUDGET:
                warnings.append(f"semantic:{name}:budget_exhausted")
                continue
            serialized = json.dumps(semantic[name], sort_keys=True, ensure_ascii=True)
            source = _snapshot(
                f"pados://{scope_id}/{name}", "semantic", serialized.encode("utf-8"), limit
            )
            sources.append(source)
            remaining -= source.bytes_read

        files = workspace_fs(self.workspace)
        for name in DEFAULT_SOURCES:
            limit = min(remaining, self.max_source_bytes)
            if limit < _MIN_BUDGET:
                warnings.append(f"{name}:budget_exhausted")
                continue
            try:
                raw = files.read_file(name, max_bytes=limit, allow_truncate=True)
            except SafeFSError as exc:
                if exc.code == "SAFE_FS_NOT_REGULAR_FILE":
                    warnings.append(f"{name}:not_regular")
                else:
                    warnings.append(f"{name}:unreadable_or_symlink")
                continue
            except OSError:
                warnings.append(f"{name}:unreadable_or_symlink")
                continue
            if raw is None:
                continue
            source = _snapshot(name, "repository", raw, limit)
            sources.append(source)
            remaining -= source.bytes_read

        return ProjectContext(tuple(sources), scope_id, scope_version, tuple(warnings))
