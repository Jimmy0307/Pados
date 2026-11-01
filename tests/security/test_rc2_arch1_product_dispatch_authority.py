"""RC2 ARCH-1 / SEC-1: Product Loop dispatch and its CAS commit never run on a self-asserted flag.

``GoalRequest.authorized`` is the caller asking for dispatch; it is not authority. Dispatch needs an APPROVED approval
whose ``proposal_hash`` is the canonical hash of exactly this operation, verified by the state gateway, and the same
approval is verified again immediately before the semantic CAS commit.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from pados.product_loop.authorization import (
    APPROVAL_REF_NOT_APPROVED,
    APPROVAL_SUBJECT_MISMATCH,
    ProductAuthorizationError,
    product_dispatch_hash,
)
from pados.product_loop.maker import FileDirectMaker
from pados.product_loop.schemas import GoalRequest, ProductLoopStatus, RuntimeResult, WorkContract
from pados.product_loop.service import ProductLoopService
from pados.product_loop.state import InMemoryP6_19BStateGateway
from pados.product_loop.validation import FileValidator


class _CountingMaker:
    def __init__(self, inner: FileDirectMaker) -> None:
        self.inner = inner
        self.approved_root = inner.approved_root
        self.calls = 0

    async def make(self, contract: WorkContract, *, run_id: str, attempt: int) -> RuntimeResult:
        self.calls += 1
        return await self.inner.make(contract, run_id=run_id, attempt=attempt)


def _request(root: Path, **overrides: Any) -> GoalRequest:
    values: dict[str, Any] = {
        "goal": "Implement the bounded fixture change.",
        "workspace_root": str(root),
        "scope_id": f"scope-{root.name}",
        "changes": [{"path": "app.py", "content": "def result():\n    return 'good'\n"}],
        "acceptance_criteria": [{"criterion_id": "good", "check": "FILE_CONTAINS", "target_path": "app.py",
                                 "expected_text": "return 'good'"}],
        "authorized": True,
    }
    values.update(overrides)
    return GoalRequest.model_validate(values)


def _service(root: Path) -> tuple[ProductLoopService, InMemoryP6_19BStateGateway, _CountingMaker]:
    gateway = InMemoryP6_19BStateGateway(root / "objects")
    maker = _CountingMaker(FileDirectMaker(root, protected_roots=(Path("C:/PADOS"), Path("C:/PADOS_WORKTREES"))))
    return ProductLoopService(maker=maker, validator=FileValidator(), state_gateway=gateway), gateway, maker


def test_the_authorized_flag_alone_never_dispatches_or_commits(tmp_path: Path) -> None:
    service, gateway, maker = _service(tmp_path)
    request = _request(tmp_path)
    report = asyncio.run(service.execute(request))
    assert report.status == ProductLoopStatus.AUTHORITY_BLOCK
    assert maker.calls == 0
    assert gateway.versions.get(request.scope_id, 0) == 0 and gateway.commits == {}


def test_an_approval_for_another_operation_is_refused_before_dispatch(tmp_path: Path) -> None:
    service, gateway, maker = _service(tmp_path)
    approved = _request(tmp_path)
    ref = gateway.grant_approval(approved.scope_id, product_dispatch_hash(approved))
    # Same scope, different file content: the approval does not name this operation.
    other = _request(tmp_path, changes=[{"path": "app.py", "content": "import os\n"}], approval_ref=ref)
    with pytest.raises(ProductAuthorizationError) as refused:
        asyncio.run(service.execute(other))
    assert refused.value.code == APPROVAL_SUBJECT_MISMATCH
    assert maker.calls == 0 and gateway.commits == {}


def test_an_approval_revoked_before_the_commit_stops_the_cas(tmp_path: Path) -> None:
    service, gateway, maker = _service(tmp_path)
    request = _request(tmp_path)
    ref = gateway.grant_approval(request.scope_id, product_dispatch_hash(request))
    original = maker.make

    async def make_then_revoke(contract: WorkContract, *, run_id: str, attempt: int) -> RuntimeResult:
        result = await original(contract, run_id=run_id, attempt=attempt)
        gateway.revoke_approval(ref)  # the human withdraws the approval while the work runs
        return result

    maker.make = make_then_revoke  # type: ignore[method-assign]
    report = asyncio.run(service.execute(request.model_copy(update={"approval_ref": ref})))
    assert maker.calls == 1
    assert report.status == ProductLoopStatus.AUTHORITY_BLOCK
    assert report.semantic_state == "NOT_COMMITTED"
    assert gateway.versions.get(request.scope_id, 0) == 0 and gateway.commits == {}


def test_a_not_approved_reference_is_refused_before_dispatch(tmp_path: Path) -> None:
    service, gateway, maker = _service(tmp_path)
    request = _request(tmp_path)
    ref = gateway.grant_approval(request.scope_id, product_dispatch_hash(request))
    gateway.revoke_approval(ref)
    with pytest.raises(ProductAuthorizationError) as refused:
        asyncio.run(service.execute(request.model_copy(update={"approval_ref": ref})))
    assert refused.value.code == APPROVAL_REF_NOT_APPROVED
    assert maker.calls == 0


def test_an_approval_bound_to_this_exact_operation_dispatches_and_commits(tmp_path: Path) -> None:
    service, gateway, maker = _service(tmp_path)
    request = _request(tmp_path)
    ref = gateway.grant_approval(request.scope_id, product_dispatch_hash(request))
    report = asyncio.run(service.execute(request.model_copy(update={"approval_ref": ref})))
    assert report.status == ProductLoopStatus.COMMITTED
    assert maker.calls == 1 and gateway.versions[request.scope_id] == 1
    assert report.contracts[0].approval_ref == ref


def test_the_dispatch_hash_names_the_operation_not_the_server_context_or_flag(tmp_path: Path) -> None:
    request = _request(tmp_path)
    assert product_dispatch_hash(request) == product_dispatch_hash(
        request.model_copy(update={"context": {"server": "filled"}, "approval_ref": "x", "authorized": False}))
    for change in ({"goal": "another goal"}, {"scope_id": "scope-other"}, {"max_attempts": 2},
                   {"human_gate": True}, {"idempotency_key": "another-key"}):
        assert product_dispatch_hash(request.model_copy(update=change)) != product_dispatch_hash(request)
