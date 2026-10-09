from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from pados.config import get_settings
from pados.db import get_engine, session_scope
from pados.memory.service import bootstrap_project
from pados.product_loop.maker import FileDirectMaker
from pados.product_loop.schemas import GoalRequest
from pados.product_loop.service import ProductLoopService
from pados.product_loop.state import P6_19BStateGateway
from pados.product_loop.validation import FileValidator
from pados.state_proof import ObjectStore, StateProofService, repository


def _registered_scope(root: Path) -> str:
    """Bind the run to a registered project with an initialized ScopeVersion.

    The product loop resolves semantic context from the registered project row
    whose root matches the workspace; free-form scope strings fail closed.
    """
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    with session_scope() as session:
        project_id = session.execute(
            text("SELECT project_id::text FROM context_project_registry WHERE canonical_root = :root"),
            {"root": str(resolved)},
        ).scalar()
        if project_id is None:
            project_id = bootstrap_project(session, name=f"p6-21g-{uuid4()}", root_path=resolved)
        repository.ensure_scope(session, project_id)
    return str(project_id)


def _pin_context_snapshot(service: ProductLoopService, scope_id: str) -> None:
    """Serve every later run the context read now, before another writer commits.

    The second candidate therefore carries an expected ScopeVersion that the
    authoritative head has already moved past, and CAS must reject it.
    """
    snapshot = service.state_gateway.resolve_project_context(scope_id)
    service.state_gateway.resolve_project_context = lambda _scope_id: snapshot


def _request(root: Path, *, scope_id: str, content: str, max_attempts: int = 1) -> GoalRequest:
    return GoalRequest(
        goal="Apply and verify one isolated candidate file change.",
        workspace_root=str(root),
        scope_id=scope_id,
        changes=[{"path": "app.py", "content": content}],
        acceptance_criteria=[
            {
                "criterion_id": "contains-good",
                "check": "FILE_CONTAINS",
                "target_path": "app.py",
                "expected_text": "return 'good'",
            },
            {
                "criterion_id": "compile",
                "operation": "TEST",
                "check": "PYTHON_COMPILE",
                "target_path": "app.py",
            },
        ],
        authorized=True,
        max_attempts=max_attempts,
    )


@pytest.fixture
def p6_19b_product_service(tmp_path: Path):
    settings = get_settings()
    engine = get_engine(settings)
    proof = StateProofService(engine, ObjectStore(tmp_path / "objects"))
    service = ProductLoopService(
        maker=FileDirectMaker(
            tmp_path / "workspace",
            protected_roots=(Path("C:/PADOS"), Path("C:/PADOS_WORKTREES")),
        ),
        validator=FileValidator(),
        state_gateway=P6_19BStateGateway(proof),
    )
    yield service, engine
    engine.dispose()


def test_p6_21g_happy_and_self_correction_use_p6_19b_receipts(
    p6_19b_product_service,
) -> None:
    service, engine = p6_19b_product_service
    workspace = Path(service.maker.approved_root)
    with engine.connect() as connection:
        before = {
            table: int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
            for table in ("state_proof_objects", "state_proof_results", "state_proof_commits")
        }
    request = _request(
        workspace,
        scope_id=_registered_scope(workspace),
        content="def result():\n    return 'bad'\n",
        max_attempts=2,
    )
    request = GoalRequest.model_validate(
        {
            **request.model_dump(mode="json"),
            "revision_changes": [
                {"path": "app.py", "content": "def result():\n    return 'good'\n"}
            ],
        }
    )
    failed_then_revised = asyncio.run(
        service.execute(request)
    )
    assert failed_then_revised.status.value == "COMMITTED"
    assert len(failed_then_revised.attempts) == 2
    assert failed_then_revised.model_dump(mode="json")["status"] == "COMMITTED"
    assert failed_then_revised.attempts[0].receipt.receipt_id != failed_then_revised.attempts[1].receipt.receipt_id
    assert all(attempt.receipt.output_content_hash for attempt in failed_then_revised.attempts)

    with engine.connect() as connection:
        counts = {
            table: int(
                connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
            )
            for table in ("state_proof_objects", "state_proof_results", "state_proof_commits")
        }
    assert counts["state_proof_results"] - before["state_proof_results"] == 2
    assert counts["state_proof_commits"] - before["state_proof_commits"] == 1
    assert counts["state_proof_objects"] - before["state_proof_objects"] >= 4


def test_p6_21g_stale_candidate_retains_receipt_in_p6_19b(
    p6_19b_product_service,
) -> None:
    service, engine = p6_19b_product_service
    workspace = Path(service.maker.approved_root)
    scope_id = _registered_scope(workspace)
    with engine.connect() as connection:
        before_results = int(
            connection.execute(text("SELECT count(*) FROM state_proof_results")).scalar_one()
        )
        before_stale = int(
            connection.execute(
                text("SELECT count(*) FROM state_proof_commits WHERE status = 'STALE_CANDIDATE'")
            ).scalar_one()
        )
    _pin_context_snapshot(service, scope_id)
    first = asyncio.run(
        service.execute(
            _request(
                workspace,
                scope_id=scope_id,
                content="def result():\n    return 'good'\n",
            )
        )
    )
    second = asyncio.run(
        service.execute(
            _request(
                workspace,
                scope_id=scope_id,
                content="def result():\n    return 'good'\n",
            )
        )
    )
    assert first.status.value == "COMMITTED"
    assert second.status.value == "STALE_CANDIDATE"
    assert second.commit is not None and second.commit["status"] == "STALE_CANDIDATE"
    assert second.attempts[0].receipt.output_content_hash

    with engine.connect() as connection:
        result_count = int(connection.execute(text("SELECT count(*) FROM state_proof_results")).scalar_one())
        stale_count = int(
            connection.execute(
                text("SELECT count(*) FROM state_proof_commits WHERE status = 'STALE_CANDIDATE'")
            ).scalar_one()
        )
    assert result_count - before_results == 2
    assert stale_count - before_stale == 1
