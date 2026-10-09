"""CF-03 (CR-13c): post-execution revalidation is the fabric's linearization point, and same-thread registry
mutation during a dispatch is refused by a dispatch-in-progress flag (not by reentrancy).

Concurrency uses events only (``threading.Event.wait`` with a generous upper bound that is never expected to
elapse); no test sleeps."""
from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fabric_support import (
    OPERATOR,
    PROJECT,
    Delegate,
    Fabric,
    Inventory,
    TransformAdapter,
    adapter_descriptor,
    fabric,
    with_model,
)

from pados.capability.executor import ExecutionStatus
from pados.capability.model import LifecycleState
from pados.capability.registry import RegistryError
from pados.enterprise.classification import DataClassification

WAIT = 60.0


class MutableProvenance:
    """A trusted provenance seam whose answer can change (the operator's store)."""

    def __init__(self, entries: dict[str, Any]) -> None:
        self.entries = entries

    def resolve(self, sha256: str) -> Any:
        return self.entries.get(sha256)


def counting_schema(fab: Fabric) -> list[int]:
    """Wrap the OCR schema validator: proves that a withheld output is never verified."""
    calls: list[int] = []
    _, validator = fab.schemas._checks["document.ocr/v1"]

    def counted(outputs: Any) -> bool:
        calls.append(1)
        return validator(outputs)

    fab.schemas._checks["document.ocr/v1"] = ("ocr-schema", counted)
    return calls


def assert_withheld(fab: Fabric, result: Any, stage: str, verified: list[int] | None = None) -> None:
    index = [s.stage_id for s in result.plan_receipt.stages].index(stage)
    receipt = result.plan_receipt.stages[index]
    assert result.status is ExecutionStatus.FAILED
    assert result.failure_code == f"{stage}:REVOKED_DURING_EXECUTION"
    assert receipt.failure_code == "REVOKED_DURING_EXECUTION" and receipt.outputs == ()
    assert receipt.verification.value == "NOT_VERIFIED" and receipt.verifier_id is None
    assert stage not in result.outputs  # no output of the call is referenced anywhere
    if verified is not None:
        assert verified == []  # refused BEFORE any verification
    later = [s for s in result.plan_receipt.stages[index + 1:]]
    assert all(s.failure_code == "NOT_EXECUTED_UPSTREAM_FAILED" and s.outputs == () for s in later)


# --- changes from inside the adapter body (injected hook) --------------------------------------------


DURING_OCR: dict[str, Callable[[Fabric, Inventory, MutableProvenance], None]] = {
    "grant_revoked": lambda fab, inv, prov: fab.grants.grant("TEXT_CHUNK"),
    "grant_document_changed": lambda fab, inv, prov: fab.grants.grants[(PROJECT, "wc-1")].update(approval_ref="x2"),
    "inventory_removed": lambda fab, inv, prov: inv.fits.update({"local-cpu": False}),
    "provenance_changed": lambda fab, inv, prov: prov.entries.update(
        {fab.scan.sha256: (PROJECT, DataClassification.RESTRICTED)}),
    "provenance_removed": lambda fab, inv, prov: prov.entries.clear(),
}


@pytest.mark.parametrize("name", sorted(DURING_OCR))
def test_authority_change_during_execution_withholds_every_output(tmp_path: Path, name: str) -> None:
    fab, inventory = fabric(tmp_path), Inventory()
    provenance = MutableProvenance({fab.scan.sha256: (PROJECT, DataClassification.CONFIDENTIAL)})
    task = fab.task()
    plan = fab.planner(inventory=inventory, provenance=provenance).plan(task)
    verified = counting_schema(fab)
    ocr = fab.adapters["ocr.alpha"]
    ocr.hook = lambda: DURING_OCR[name](fab, inventory, provenance)
    result = fab.executor(inventory=inventory, provenance=provenance).execute(plan, task)
    assert len(ocr.calls) == 1  # the call ran (it was authorized when it started) ...
    assert_withheld(fab, result, "ocr", verified)  # ... but nothing it produced is used
    assert not fab.adapters["chunk.fixed"].calls


def test_router_selection_change_during_the_model_call_withholds_its_output(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    task = with_model(fab)
    delegate = Delegate()
    plan = fab.planner(inference=delegate).plan(task)
    model = fab.adapters["llm.bridge"]
    model.hook = lambda: setattr(delegate, "worker", "claude-remote")
    result = fab.executor(inference=delegate).execute(plan, task)
    assert len(model.calls) == 1
    assert_withheld(fab, result, "summary")
    assert "ocr" in result.outputs and "chunk" in result.outputs  # earlier, unaffected stages keep theirs


def test_descriptor_swapped_by_the_adapter_during_execution_withholds_outputs(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    ocr = fab.adapters["ocr.alpha"]
    ocr.hook = lambda: setattr(ocr, "descriptor", adapter_descriptor(
        "ocr.alpha", "DOCUMENT_OCR", max_classification=DataClassification.PUBLIC))
    result = fab.executor().execute(plan, task)
    assert_withheld(fab, result, "ocr")


def test_fallback_alternative_output_is_withheld_when_revoked_during_its_call(tmp_path: Path) -> None:
    fab = fabric(tmp_path, ocr_fallback=True)
    fab.adapters["ocr.alpha"].fail_with = "ENGINE_CRASHED"
    beta = fab.add(TransformAdapter(adapter_descriptor("ocr.beta", "DOCUMENT_OCR"), kind="ocr.text"))
    task = fab.task()
    plan = fab.planner().plan(task)
    beta.hook = lambda: fab.grants.grant("TEXT_CHUNK")
    result = fab.executor().execute(plan, task)
    assert len(beta.calls) == 1 and result.plan_receipt.stages[0].adapter_id == "ocr.beta"
    assert result.plan_receipt.stages[0].fallback_transition_hash is not None
    assert_withheld(fab, result, "ocr")


def test_unchanged_authorities_keep_the_outputs(tmp_path: Path) -> None:
    fab, inventory = fabric(tmp_path), Inventory()
    task = fab.task()
    plan = fab.planner(inventory=inventory).plan(task)
    result = fab.executor(inventory=inventory).execute(plan, task)
    assert result.status is ExecutionStatus.COMPLETED and result.outputs["ocr"] and result.outputs["chunk"]


# --- another thread's revocation while the call runs (events only) -----------------------------------


def test_grant_revoked_from_another_thread_while_the_call_runs_withholds_outputs(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    started, revoked = threading.Event(), threading.Event()

    def body() -> None:  # the adapter is mid-call: it waits until the operator's revocation has completed
        started.set()
        assert revoked.wait(WAIT)

    fab.adapters["ocr.alpha"].hook = body

    def operator() -> None:
        assert started.wait(WAIT)
        fab.grants.grant("TEXT_CHUNK")  # another authority (not the registry): does not wait for the lock
        revoked.set()

    thread = threading.Thread(target=operator, name="operator")
    thread.start()
    result = fab.executor().execute(plan, task)
    thread.join(WAIT)
    assert not thread.is_alive()
    assert_withheld(fab, result, "ocr")


def test_registry_revocation_from_another_thread_is_ordered_after_the_post_check(tmp_path: Path) -> None:
    """The registry mutator waits on the dispatch lock, so it can only complete after the post-execution check
    (which therefore saw the unrevoked state): the output was taken first, the revocation applies to later
    dispatches."""
    fab = fabric(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    started, attempting = threading.Event(), threading.Event()
    order: list[str] = []

    def body() -> None:
        started.set()
        assert attempting.wait(WAIT)
        order.append("call_returned")

    fab.adapters["ocr.alpha"].hook = body
    lock = fab.registry._dispatch

    class Signalling:
        def __enter__(self) -> None:
            if threading.current_thread().name == "operator":
                attempting.set()
            lock.acquire()

        def __exit__(self, *exc: object) -> None:
            lock.release()

    fab.registry._dispatch = Signalling()  # type: ignore[assignment]

    def operator() -> None:
        assert started.wait(WAIT)
        fab.registry.set_adapter_lifecycle("ocr.alpha", LifecycleState.REVOKED, operator=OPERATOR,
                                           reason_code="SECURITY_ADVISORY")
        order.append("revoked")

    thread = threading.Thread(target=operator, name="operator")
    thread.start()
    result = fab.executor().execute(plan, task)
    thread.join(WAIT)
    assert not thread.is_alive() and order == ["call_returned", "revoked"]
    assert result.outputs["ocr"]  # accepted at the linearization point, before the revocation completed
    assert fab.registry.adapter("ocr.alpha").recorded_descriptor().lifecycle is LifecycleState.REVOKED


# --- same-thread registry mutation during a dispatch ------------------------------------------------


def _revoke(fab: Fabric, adapter_id: str = "chunk.fixed") -> None:
    fab.registry.set_adapter_lifecycle(adapter_id, LifecycleState.REVOKED, operator=OPERATOR,
                                       reason_code="SECURITY_ADVISORY")


SAME_THREAD: dict[str, Callable[[Fabric], None]] = {
    "revoke_adapter": _revoke,
    "deprecate_capability": lambda fab: fab.registry.set_capability_lifecycle(
        "TEXT_CHUNK", LifecycleState.DEPRECATED, operator=OPERATOR, reason_code="SUPERSEDED"),
    "install": lambda fab: fab.registry.install(TransformAdapter(adapter_descriptor("ocr.late", "DOCUMENT_OCR")),
                                                operator=OPERATOR),
}


@pytest.mark.parametrize("where", ["available", "execute", "execute_swallowed", "grant_seam"])
@pytest.mark.parametrize("mutation", sorted(SAME_THREAD))
def test_same_thread_registry_mutation_during_dispatch_is_refused(tmp_path: Path, where: str, mutation: str,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    fab = fabric(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    ocr = fab.adapters["ocr.alpha"]
    seen: list[str] = []
    events_before = len(fab.registry.events)

    def attempt() -> None:
        try:
            SAME_THREAD[mutation](fab)
        except RegistryError as exc:
            seen.append(exc.code)
            if where != "execute_swallowed":
                raise

    if where == "available":
        def probe() -> None:  # planning and prepare also probe; only the guarded (dispatch) probe mutates
            if getattr(fab.registry._in_dispatch, "depth", 0) and not seen:
                attempt()

        ocr.probe_hook = probe
    elif where in {"execute", "execute_swallowed"}:
        ocr.hook = attempt
    else:
        original = fab.grants.current_grant
        armed: list[bool] = []
        ocr.hook = lambda: armed.append(True)  # arm after the call started: the post-check reads the grant

        def grant(**kwargs: Any) -> Any:
            if armed and not seen:
                attempt()
            return original(**kwargs)

        monkeypatch.setattr(fab.grants, "current_grant", grant)
    result = fab.executor().execute(plan, task)
    assert seen == ["REGISTRY_MUTATION_DURING_DISPATCH"]
    assert result.failure_code == "ocr:REGISTRY_MUTATION_DURING_DISPATCH"
    assert result.plan_receipt.stages[0].outputs == () and "ocr" not in result.outputs
    assert len(fab.registry.events) == events_before  # the mutation never happened
    assert fab.registry.adapter("chunk.fixed").recorded_descriptor().lifecycle is LifecycleState.ACTIVE
    assert fab.registry.capability("TEXT_CHUNK").lifecycle is LifecycleState.ACTIVE
    assert not fab.registry.is_installed("ocr.late")
    SAME_THREAD[mutation](fab)  # the flag is cleared once the dispatch is over: the operator call works now

