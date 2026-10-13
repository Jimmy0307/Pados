from pathlib import Path

from sqlalchemy import text

from pados.core.task_service import create_project
from pados.db import session_scope
from pados.retrieval.indexer import index_repository, search_chunks
from pados.wiki.service import mark_stale_pages, upsert_wiki_page, write_module_wiki_page


def test_repository_index_secret_skip_and_fts() -> None:
    root = Path("tests/fixtures/sample_repo").resolve()
    with session_scope() as session:
        project_id = create_project(session, name="p2-index-project", root_path=str(root))
        indexed = index_repository(session, project_id=project_id, root=root)
        paths = {item.relative_path: item for item in indexed}
        assert paths["app.py"].chunks == 1
        assert paths["secret_sample.txt"].is_secret_suspected is True
        assert paths["secret_sample.txt"].chunks == 0
        results = search_chunks(session, project_id=project_id, query="greet")
        assert results
        assert results[0]["relative_path"] == "app.py"


def test_repository_index_rejects_root_outside_project_boundary(tmp_path: Path) -> None:
    root = Path("tests/fixtures/sample_repo").resolve()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.py").write_text("print('external')\n", encoding="utf-8")
    with session_scope() as session:
        project_id = create_project(session, name="p6-6-boundary-project", root_path=str(root))
        try:
            index_repository(session, project_id=project_id, root=outside)
        except PermissionError:
            pass
        else:
            raise AssertionError("expected index_repository to reject outside root")


def test_wiki_page_stale_detection(tmp_path: Path) -> None:
    root = Path("tests/fixtures/sample_repo").resolve()
    with session_scope() as session:
        project_id = create_project(session, name="p2-wiki-project", root_path=str(root))
        indexed = index_repository(session, project_id=project_id, root=root)
        app_file = next(item for item in indexed if item.relative_path == "app.py")
        page_path = write_module_wiki_page(
            wiki_root=tmp_path / "wiki",
            slug="sample-app",
            title="Sample App",
            source_path=app_file.relative_path,
            source_hash=app_file.sha256,
        )
        page_id = upsert_wiki_page(
            session,
            project_id=project_id,
            slug="sample-app",
            title="Sample App",
            category="modules",
            markdown_path=page_path.as_posix(),
            content_hash=app_file.sha256,
            source_commit="test",
        )
        file_id = session.execute(
            text(
                """
                SELECT id FROM file_registry
                WHERE project_id = :project_id AND relative_path = 'app.py'
                """
            ),
            {"project_id": project_id},
        ).scalar_one()
        session.execute(
            text(
                """
                INSERT INTO wiki_sources
                  (wiki_page_id, file_id, source_hash, relationship_type)
                VALUES (:page_id, :file_id, :source_hash, 'documents')
                """
            ),
            {"page_id": page_id, "file_id": file_id, "source_hash": "old-hash"},
        )
        assert mark_stale_pages(session, project_id=project_id) == 1

