from pathlib import Path

import pytest

from pados.agents.loop import run_single_agent_loop, validate_scope
from pados.core.task_service import create_project
from pados.db import session_scope
from pados.providers.mock import MockProvider


def test_single_agent_loop_creates_artifact_and_checkpoint() -> None:
    repo_root = Path("tests/fixtures/sample_repo").resolve()
    artifact_root = Path("runtime/artifacts/p3").resolve()
    with session_scope() as session:
        project_id = create_project(session, name="p3-loop-project", root_path=str(repo_root))
        result = run_single_agent_loop(
            session,
            project_id=project_id,
            repository_root=repo_root,
            artifact_root=artifact_root,
            forbidden_paths=[repo_root / "secret_sample.txt"],
            git_commit="test",
            provider=MockProvider(),
            provider_name="mock",
        )
        assert result["packet_hash"]
        assert Path(result["artifact_path"]).exists()
        assert result["checkpoint_id"]


def test_scope_validation_rejects_forbidden_path() -> None:
    root = Path("runtime/artifacts/p3").resolve()
    forbidden = root / "blocked"
    with pytest.raises(PermissionError):
        validate_scope(target_path=forbidden / "x.txt", allowed_root=root, forbidden_paths=[forbidden])

