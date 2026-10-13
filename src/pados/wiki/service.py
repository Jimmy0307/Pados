# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session


def upsert_wiki_page(
    session: Session,
    *,
    project_id: str,
    slug: str,
    title: str,
    category: str,
    markdown_path: str,
    content_hash: str,
    source_commit: str,
) -> str:
    page_id = session.execute(
        text(
            """
            INSERT INTO wiki_pages
              (project_id, slug, title, category, status, confidence,
               markdown_path, content_hash, source_commit, last_verified_at)
            VALUES
              (:project_id, :slug, :title, :category, 'active', 'verified',
               :markdown_path, :content_hash, :source_commit, now())
            ON CONFLICT (project_id, slug)
            DO UPDATE SET
              title = EXCLUDED.title,
              category = EXCLUDED.category,
              status = 'active',
              confidence = 'verified',
              markdown_path = EXCLUDED.markdown_path,
              content_hash = EXCLUDED.content_hash,
              source_commit = EXCLUDED.source_commit,
              last_verified_at = now(),
              stale_at = NULL
            RETURNING id
            """
        ),
        {
            "project_id": project_id,
            "slug": slug,
            "title": title,
            "category": category,
            "markdown_path": markdown_path,
            "content_hash": content_hash,
            "source_commit": source_commit,
        },
    ).scalar_one()
    return str(page_id)


def mark_stale_pages(session: Session, *, project_id: str) -> int:
    rows = session.execute(
        text(
            """
            UPDATE wiki_pages wp
            SET status = 'stale', stale_at = now()
            WHERE project_id = :project_id
              AND EXISTS (
                SELECT 1
                FROM wiki_sources ws
                JOIN file_registry fr ON fr.id = ws.file_id
                WHERE ws.wiki_page_id = wp.id
                  AND ws.source_hash <> fr.sha256
              )
            RETURNING id
            """
        ),
        {"project_id": project_id},
    ).scalars().all()
    return len(rows)


def write_module_wiki_page(
    *,
    wiki_root: Path,
    slug: str,
    title: str,
    source_path: str,
    source_hash: str,
) -> Path:
    module_dir = wiki_root / "modules"
    module_dir.mkdir(parents=True, exist_ok=True)
    path = module_dir / f"{slug}.md"
    path.write_text(
        "\n".join(
            [
                "---",
                f"title: {title}",
                f"slug: {slug}",
                "category: modules",
                "status: active",
                "confidence: verified",
                "source_files:",
                f"  - {source_path}",
                "source_commit:",
                "source_hashes:",
                f"  - {source_hash}",
                "last_verified: 2026-07-10",
                "owner_role: repo_mapper",
                "supersedes:",
                "---",
                "",
                f"# {title}",
                "",
                f"Verified source: `{source_path}`",
            ]
        ),
        encoding="utf-8",
    )
    return path

