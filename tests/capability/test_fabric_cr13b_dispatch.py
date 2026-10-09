"""CF-03 (CR-13b): the final recheck and the adapter call are one linearization point with registry changes.

Concurrency is driven with events only: the operator thread signals when it is AT the registry dispatch lock
(an instrumented wrapper around the registry's own RLock), so no test depends on timing."""
from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fabric_support import (
    OPERATOR,
    Delegate,
    Fabric,
    Inventory,
    TransformAdapter,
    adapter_descriptor,
    capability,
    fabric,
    with_model,
)

import pados.capability.executor as executor_module
from pados.capability.executor import ExecutionRefused, ExecutionStatus
from pados.capability.model import LifecycleState
from pados.capability.provenance import InMemoryInputProvenance

WAIT = 60.0  # upper bound for event waits that are expected to fire (never a sleep)


class InstrumentedLock:
    """Wraps the registry's dispatch RLock: records who enters and signals when another thread is at it."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.other_thread_waiting = threading.Event()
        self.entries: list[str] = []
        self._local = threading.local()

    def held(self) -> bool:
        return getattr(self._local, "depth", 0) > 0

    def __enter__(self) -> InstrumentedLock:
        if threading.current_thread() is not threading.main_thread():
            self.other_thread_waiting.set()
        self.inner.acquire()
        self._local.depth = getattr(self._local, "depth", 0) + 1
        self.entries.append(threading.current_thread().name)
        return self

    def __exit__(self, *exc: object) -> None:
        self._local.depth -= 1
        self.inner.release()


def instrument(fab: Fabric, monkeypatch: pytest.MonkeyPatch) -> InstrumentedLock:
    lock = InstrumentedLock(fab.registry._dispatch)
    monkeypatch.setattr(fab.registry, "_dispatch", lock)
    return lock


def once(action: Callable[[], None]) -> Callable[[], None]:
    done: list[bool] = []

    def run() -> None:
        if not done:
            done.append(True)
            action()

    return run


def revoke_alpha(fab: Fabric) -> None:
    fab.registry.set_adapter_lifecycle("ocr.alpha", LifecycleState.REVOKED, operator=OPERATOR,
                                       reason_code="SECURITY_ADVISORY")


def lifecycle(fab: Fabric) -> str:
    return fab.registry.adapter("ocr.alpha").recorded_descriptor().lifecycle.value


def test_cf03_revocation_during_the_final_check_waits_until_the_call_has_run(tmp_path: Path,
                                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    fab = fabric(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    lock = instrument(fab, monkeypatch)
    order: list[str] = []
    check_in_progress = threading.Event()
    original = executor_module._Run.recheck

    def recheck(self: Any, stage: Any, binding: Any, ready: bool) -> None:
        original(self, stage, binding, ready)  # the authoritative check has passed ...
        if stage.stage_id == "ocr" and not check_in_progress.is_set():
            check_in_progress.set()  # ... and the operator now tries to revoke before the call
            assert lock.other_thread_waiting.wait(WAIT)
            order.append("check_passed")

    monkeypatch.setattr(executor_module._Run, "recheck", recheck)
    fab.adapters["ocr.alpha"].hook = once(lambda: order.append(f"adapter_ran:{lifecycle(fab)}"))

    def operator() -> None:
        assert check_in_progress.wait(WAIT)
        revoke_alpha(fab)
        order.append("revoked")

    thread = threading.Thread(target=operator, name="operator")
    thread.start()
    result = fab.executor().execute(plan, task)
    thread.join(WAIT)
    assert not thread.is_alive()
    assert order == ["check_passed", "adapter_ran:ACTIVE", "revoked"]  # blocked until the call had run
    assert result.status is ExecutionStatus.COMPLETED and "operator" in lock.entries
    assert lifecycle(fab) == "REVOKED"
    with pytest.raises(ExecutionRefused):  # every later dispatch sees the completed revocation
        fab.executor().execute(plan, task)


def test_cf03_revocation_completed_before_the_final_check_refuses(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    fab = fabric(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    lock = instrument(fab, monkeypatch)
    impl = fab.adapters["ocr.alpha"]
    finished: list[bool] = []

    def revoke_from_another_thread() -> None:  # runs inside available(): outside the dispatch lock
        thread = threading.Thread(target=lambda: (revoke_alpha(fab), finished.append(True)), name="operator")
        thread.start()
        thread.join(WAIT)
        assert finished == [True]

    original = executor_module._Run.prepare

    def prepare(self: Any, stage: Any, *args: Any) -> Any:
        prepared = original(self, stage, *args)
        if stage.stage_id == "ocr":
            impl.probe_hook = once(revoke_from_another_thread)
        return prepared

    monkeypatch.setattr(executor_module._Run, "prepare", prepare)
    result = fab.executor().execute(plan, task)
    assert result.failure_code == "ocr:LIFECYCLE_CHANGED_BEFORE_EXECUTION"
    assert not impl.calls and not fab.adapters["chunk.fixed"].calls and "operator" in lock.entries


def test_cf03_every_registry_mutator_takes_the_dispatch_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fab = fabric(tmp_path)
    lock = instrument(fab, monkeypatch)
    mutations: dict[str, Callable[[], Any]] = {
        "register_capability": lambda: fab.registry.register_capability(capability("NEW_CAP"), operator=OPERATOR),
        "install": lambda: fab.registry.install(TransformAdapter(adapter_descriptor("ocr.late", "DOCUMENT_OCR")),
                                                operator=OPERATOR),
        "register_adapter": lambda: fab.registry.register_adapter("ocr.late", capability_id="DOCUMENT_OCR",
                                                                  operator=OPERATOR),
        "set_adapter_lifecycle": lambda: fab.registry.set_adapter_lifecycle(
            "ocr.late", LifecycleState.DEPRECATED, operator=OPERATOR, reason_code="SUPERSEDED"),
        "set_capability_lifecycle": lambda: fab.registry.set_capability_lifecycle(
            "NEW_CAP", LifecycleState.REVOKED, operator=OPERATOR, reason_code="SECURITY_ADVISORY"),
    }
    for name, mutate in mutations.items():
        before = len(lock.entries)
        mutate()
        assert len(lock.entries) == before + 1, name
        assert not lock.held(), name


def test_cf03_external_seams_are_reread_inside_the_lock_right_before_the_call(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fab = fabric(tmp_path)
    task = with_model(fab)
    lock = instrument(fab, monkeypatch)
    calls: list[tuple[str, bool]] = []
    inventory, delegate = Inventory(), Delegate()
    provenance = fab.provenance
    grants_read = fab.grants.current_grant

    def grant(**kwargs: Any) -> Any:
        calls.append(("grant", lock.held()))
        return grants_read(**kwargs)

    class SeamInventory:
        def match(self, requirement: Any) -> Any:
            calls.append(("inventory", lock.held()))
            return inventory.match(requirement)

    class SeamProvenance:
        def resolve(self, sha256: str) -> Any:
            calls.append(("provenance", lock.held()))
            return InMemoryInputProvenance.resolve(provenance, sha256)

    class SeamDelegate:
        def select(self, request: Any) -> Any:
            calls.append(("inference", lock.held()))
            return delegate.select(request)

    monkeypatch.setattr(fab.grants, "current_grant", grant)
    seams: dict[str, Any] = dict(inventory=SeamInventory(), provenance=SeamProvenance(), inference=SeamDelegate())
    plan = fab.planner(**seams).plan(task)
    calls.clear()
    fab.adapters["llm.bridge"].hook = once(lambda: calls.append(("adapter", lock.held())))
    result = fab.executor(**seams).execute(plan, task)
    assert result.status is ExecutionStatus.COMPLETED
    call = calls.index(("adapter", True))  # the model adapter itself runs under the dispatch lock
    last = {name: held for name, held in calls[:call]}
    assert last == {"grant": True, "inventory": True, "provenance": True, "inference": True}, calls[:call]
