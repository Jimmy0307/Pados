import os
import zipfile
from pathlib import Path

import pytest

from pados.agents.loop import run_single_agent_loop
from pados.core import context_compiler
from pados.core.task_service import create_project, create_task, get_task, transition_task
from pados.db import session_scope
from pados.release.package import build_release
from pados.retrieval.indexer import index_repository, is_secret_suspected

PROJECT_ROOT = Path(os.environ.get("PADOS_AUDIT_ROOT", Path.cwd())).resolve()


def _ensure_release_zip(isolated: Path) -> Path:
    # Build into an isolated directory: tracked release evidence is never rewritten by tests.
    release_zip = isolated / "outputs" / "pados_release_p6_6_20260710.zip"
    build_release(PROJECT_ROOT, output_path=release_zip, reports_root=isolated / "reports")
    return release_zip


def test_illegal_task_transition_new_to_released_is_rejected() -> None:
    with session_scope() as session:
        project_id = create_project(session, name="p6-6-illegal-transition", root_path=str(PROJECT_ROOT))
        task_id = create_task(session, project_id=project_id, title="illegal", objective="state-machine")
        task = get_task(session, task_id)

        with pytest.raises(ValueError):
            transition_task(
                session,
                task_id=task.id,
                from_version=task.version,
                new_status="RELEASED",
                new_gate="G8",
                actor="adversarial-reviewer",
            )


def test_secret_detector_catches_lowercase_openai_style_keys() -> None:
    key_prefix = "s" + "k" + "-"
    probe = "tok" + "en = " + key_prefix + "proj-lowercase-example"
    assert is_secret_suspected(probe) is True


def test_context_compiler_enforces_declared_token_budget() -> None:
    packet = context_compiler.compile_context({"content": "word " * 10_000}, max_tokens=128)
    assert packet["estimated_tokens"] <= 128
    assert packet["truncated"] is True


def test_index_repository_rejects_roots_outside_registered_project_boundary(tmp_path: Path) -> None:
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    (outside_root / "outside.py").write_text("print('outside')\n", encoding="utf-8")

    with session_scope() as session:
        project_id = create_project(session, name="p6-6-root-isolation", root_path=str(PROJECT_ROOT))
        with pytest.raises(PermissionError):
            index_repository(session, project_id=project_id, root=outside_root)


def test_single_agent_loop_accepts_injected_provider_and_does_not_hardwire_mock() -> None:
    source = (PROJECT_ROOT / "src" / "pados" / "agents" / "loop.py").read_text(encoding="utf-8")
    assert "MockProvider()" not in source
    assert "provider:" in source


def test_single_agent_loop_fails_closed_without_provider() -> None:
    repo_root = PROJECT_ROOT / "tests" / "fixtures" / "sample_repo"
    artifact_root = PROJECT_ROOT / "runtime" / "artifacts" / "p6_6_adversarial"
    with session_scope() as session:
        project_id = create_project(session, name="p6-6-provider-required", root_path=str(repo_root))
        with pytest.raises(RuntimeError, match="PROVIDER_NOT_CONFIGURED"):
            run_single_agent_loop(
                session,
                project_id=project_id,
                repository_root=repo_root,
                artifact_root=artifact_root,
                forbidden_paths=[repo_root / "secret_sample.txt"],
                git_commit="test",
            )


INTERNAL_RELEASE_MANIFEST = Path(__file__).resolve().parents[2] / "config" / "release_manifest.json"
needs_release_manifest = pytest.mark.skipif(
    not INTERNAL_RELEASE_MANIFEST.is_file(),
    reason="config/release_manifest.json is an internal release manifest, not part of the public export")


@needs_release_manifest
def test_release_zip_preserves_source_tree_paths(tmp_path: Path) -> None:
    release_zip = _ensure_release_zip(tmp_path)
    with zipfile.ZipFile(release_zip) as archive:
        names = set(archive.namelist())
    assert "src/pados/cli/main.py" in names
    assert "pyproject.toml" in names


@needs_release_manifest
def test_release_zip_excludes_logs_caches_and_secret_fixtures(tmp_path: Path) -> None:
    release_zip = _ensure_release_zip(tmp_path)
    with zipfile.ZipFile(release_zip) as archive:
        names = archive.namelist()
        payload = "\n".join(names)
    assert not any(name.endswith(".pyc") for name in names)
    assert not any(name.startswith(("work/logs/", "runtime/logs/")) for name in names)
    assert "secret_sample.txt" not in payload
