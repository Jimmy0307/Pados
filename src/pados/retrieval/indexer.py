# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

SECRET_PATTERNS = (
    re.compile(r"\b(api[_-]?key|secret|token|password)\b\s*[:=]", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
IGNORED_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}


@dataclass(frozen=True)
class IndexedFile:
    relative_path: str
    sha256: str
    size_bytes: int
    is_secret_suspected: bool
    chunks: int


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def detect_language(path: Path) -> str:
    suffix_map = {
        ".py": "python",
        ".md": "markdown",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".ps1": "powershell",
        ".sql": "sql",
    }
    return suffix_map.get(path.suffix.lower(), "text")


def is_secret_suspected(text_content: str) -> bool:
    return any(pattern.search(text_content) for pattern in SECRET_PATTERNS)


def _registered_project_root(session: Session, project_id: str) -> Path:
    root_path = session.execute(
        text("SELECT root_path FROM projects WHERE id = CAST(:project_id AS uuid)"),
        {"project_id": project_id},
    ).scalar_one()
    return Path(str(root_path)).resolve()


def _validate_repository_root(session: Session, *, project_id: str, root: Path) -> Path:
    resolved_root = root.resolve()
    registered_root = _registered_project_root(session, project_id)
    if resolved_root != registered_root:
        session.execute(
            text(
                """
                INSERT INTO audit_events
                  (entity_type, entity_id, event_type, actor, before_state, after_state)
                VALUES
                  ('project', CAST(:project_id AS uuid), 'repository_root_rejected', 'repo_mapper',
                   '{}'::jsonb, jsonb_build_object('requested_root', CAST(:requested_root AS text),
                   'registered_root', CAST(:registered_root AS text)))
                """
            ),
            {
                "project_id": project_id,
                "requested_root": resolved_root.as_posix(),
                "registered_root": registered_root.as_posix(),
            },
        )
        raise PermissionError(
            f"repository root outside registered project boundary: {resolved_root}"
        )
    return resolved_root


def should_index(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in IGNORED_DIRS for part in relative.parts):
        return False
    if relative.parts[:2] == ("runtime", "logs"):
        return False
    if relative.parts[:2] == ("work", "logs"):
        return False
    return path.is_file()


def _chunks(content: str, *, max_lines: int = 80) -> list[tuple[int, int, str]]:
    lines = content.splitlines()
    result: list[tuple[int, int, str]] = []
    for index in range(0, len(lines), max_lines):
        chunk_lines = lines[index : index + max_lines]
        if not chunk_lines:
            continue
        start = index + 1
        end = index + len(chunk_lines)
        result.append((start, end, "\n".join(chunk_lines)))
    return result or [(1, 1, "")]


def mock_embedding(content: str) -> str:
    digest = hashlib.sha256(content.encode("utf-8")).digest()
    values = [round(byte / 255, 6) for byte in digest[:3]]
    return f"[{values[0]},{values[1]},{values[2]}]"


def index_file(session: Session, *, project_id: str, root: Path, path: Path) -> IndexedFile:
    payload = path.read_bytes()
    sha256 = sha256_bytes(payload)
    relative_path = path.relative_to(root).as_posix()
    try:
        content = payload.decode("utf-8")
        is_binary = False
    except UnicodeDecodeError:
        content = ""
        is_binary = True
    secret_suspected = is_secret_suspected(content)
    file_id = session.execute(
        text(
            """
            INSERT INTO file_registry
              (project_id, relative_path, language, module_name, sha256, size_bytes,
               is_binary, is_secret_suspected, last_indexed_at)
            VALUES
              (:project_id, :relative_path, :language, :module_name, :sha256, :size_bytes,
               :is_binary, :is_secret_suspected, now())
            ON CONFLICT (project_id, relative_path)
            DO UPDATE SET
              language = EXCLUDED.language,
              module_name = EXCLUDED.module_name,
              sha256 = EXCLUDED.sha256,
              size_bytes = EXCLUDED.size_bytes,
              is_binary = EXCLUDED.is_binary,
              is_secret_suspected = EXCLUDED.is_secret_suspected,
              last_indexed_at = now()
            RETURNING id
            """
        ),
        {
            "project_id": project_id,
            "relative_path": relative_path,
            "language": detect_language(path),
            "module_name": path.stem,
            "sha256": sha256,
            "size_bytes": len(payload),
            "is_binary": is_binary,
            "is_secret_suspected": secret_suspected,
        },
    ).scalar_one()
    session.execute(text("DELETE FROM file_chunks WHERE file_id = :file_id"), {"file_id": file_id})
    chunk_count = 0
    if not is_binary and not secret_suspected:
        for start, end, chunk_content in _chunks(content):
            session.execute(
                text(
                    """
                    INSERT INTO file_chunks
                      (file_id, chunk_type, symbol_name, start_line, end_line,
                       content, content_hash, embedding, metadata)
                    VALUES
                      (:file_id, 'text', NULL, :start_line, :end_line, :content,
                       :content_hash, CAST(:embedding AS vector), '{}'::jsonb)
                    """
                ),
                {
                    "file_id": file_id,
                    "start_line": start,
                    "end_line": end,
                    "content": chunk_content,
                    "content_hash": sha256_bytes(chunk_content.encode("utf-8")),
                    "embedding": mock_embedding(chunk_content),
                },
            )
            chunk_count += 1
    return IndexedFile(relative_path, sha256, len(payload), secret_suspected, chunk_count)


def index_repository(session: Session, *, project_id: str, root: Path) -> list[IndexedFile]:
    root = _validate_repository_root(session, project_id=project_id, root=root)
    indexed: list[IndexedFile] = []
    for path in sorted(root.rglob("*")):
        if should_index(path, root):
            indexed.append(index_file(session, project_id=project_id, root=root, path=path))
    return indexed


def search_chunks(session: Session, *, project_id: str, query: str, limit: int = 5) -> list[dict[str, str]]:
    rows = session.execute(
        text(
            """
            SELECT fr.relative_path, fc.start_line, fc.end_line, fc.content
            FROM file_chunks fc
            JOIN file_registry fr ON fr.id = fc.file_id
            WHERE fr.project_id = :project_id
              AND fc.search_vector @@ plainto_tsquery('english', :query)
            ORDER BY ts_rank(fc.search_vector, plainto_tsquery('english', :query)) DESC
            LIMIT :limit
            """
        ),
        {"project_id": project_id, "query": query, "limit": limit},
    ).mappings()
    return [
        {
            "relative_path": str(row["relative_path"]),
            "start_line": str(row["start_line"]),
            "end_line": str(row["end_line"]),
            "content": str(row["content"]),
        }
        for row in rows
    ]

