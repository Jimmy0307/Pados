"""CR-13 remediation: plan provenance (CF-01), trusted input provenance (CF-02), authoritative
pre-invocation recheck (CF-03), verified human decisions (CF-04), atomic registry events (CF-05)."""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fabric_support import (
    OPERATOR,
    PROJECT,
    Advisor,
    Delegate,
    Fabric,
    Inventory,
    TransformAdapter,
    adapter_descriptor,
    capability,
    fabric,
    forge,
    rehash,
    with_model,
)
from operator_support import Approver, open_approver

import pados.capability.executor as executor_module
import pados.capability.registry as registry_module
from pados.capability.executor import ExecutionRefused, ExecutionStatus
from pados.capability.graph import InputBinding, InputSource
from pados.capability.decision_ledger import FileConsumedDecisionLedger
from pados.capability.human_decision import (
    HmacHumanDecisionVerifier,
    HumanDecisionCode,
    HumanDecisionRefused,
    VerifiedHumanDecision,
    signed_payload,
)
from pados.capability.model import LifecycleState
from pados.capability.planner import AdviceRecord, ExecutionPlan, PlannerConfig, PlanStatus
from pados.capability.provenance import InMemoryInputProvenance, ProvenanceRefused
from pados.capability.registry import CapabilityRegistry, RegistryError
from pados.capability.task import TaskInput
from pados.capability.verification import VerificationEngine
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary
from pados.lifecycle.minimization import DataMinimizationViolation

# --- helpers -------------------------------------------------------------------------------------


def no_adapter_ran(fab: Fabric) -> bool:
    return all(not impl.calls for impl in fab.adapters.values())


def after_prepare(monkeypatch: pytest.MonkeyPatch, stage_id: str, action: Callable[[], None]) -> None:
    """Run ``action`` after the executor bound the stage and before the adapter is invoked."""
    original = executor_module._Run.prepare

    def patched(self: Any, stage: Any, *args: Any) -> Any:
        prepared = original(self, stage, *args)
        if stage.stage_id == stage_id:
            action()
        return prepared

    monkeypatch.setattr(executor_module._Run, "prepare", patched)


def once(action: Callable[[], None]) -> Callable[[], None]:
    done: list[bool] = []

    def run() -> None:
        if not done:
            done.append(True)
            action()

    return run


def fallback_fabric(tmp_path: Path) -> Fabric:
    fab = fabric(tmp_path, ocr_fallback=True)
    fab.add(TransformAdapter(adapter_descriptor("ocr.beta", "DOCUMENT_OCR"), kind="ocr.text"))
    fab.add(TransformAdapter(adapter_descriptor("ocr.gamma", "DOCUMENT_OCR"), kind="ocr.text"))
    fab.add(TransformAdapter(adapter_descriptor("ocr.cloud", "DOCUMENT_OCR",
                                                network_requirement=NetworkRequirement.WAN_REQUIRED)))
    return fab


# --- CF-01: the executor refuses plans it cannot reproduce ---------------------------------------


def _adapter_swap(plan: ExecutionPlan) -> ExecutionPlan:
    beta = next(c for c in plan.stages[0].candidates if c.adapter_id == "ocr.beta")
    return forge(plan, 0, selected_adapter_id="ocr.beta", adapter_digest=beta.adapter_digest,
                 decision_hash=beta.decision_hash, ordering=("ocr.beta", "ocr.alpha", "ocr.gamma"),
                 alternatives=("ocr.alpha", "ocr.gamma"))


TAMPERS: dict[str, Callable[[ExecutionPlan], ExecutionPlan]] = {
    "stage_capability": lambda plan: forge(plan, 1, capability_id="DOCUMENT_OCR"),
    "stage_removed": lambda plan: rehash(plan, plan.stages[:1]),
    "stage_seed": lambda plan: forge(plan, 1, seed=7),
    "adapter": _adapter_swap,
    "parameters": lambda plan: forge(plan, 1, parameters={"size": 4096}),
    "input_binding": lambda plan: forge(plan, 1, inputs=(InputBinding(source=InputSource.TASK_INPUT,
                                                                      name="scan"),)),
    "fallback_list_shortened": lambda plan: forge(plan, 0, alternatives=("ocr.beta",)),
    "fallback_list_widened": lambda plan: forge(plan, 0, alternatives=("ocr.beta", "ocr.gamma", "ocr.cloud")),
    "fallback_order": lambda plan: forge(plan, 0, alternatives=("ocr.gamma", "ocr.beta"),
                                         ordering=("ocr.alpha", "ocr.gamma", "ocr.beta")),
}


@pytest.mark.parametrize("name", sorted(TAMPERS))
def test_cf01_rehashed_tampered_plan_is_refused_before_any_adapter_runs(tmp_path: Path, name: str) -> None:
    fab = fallback_fabric(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    assert plan.stages[0].alternatives == ("ocr.beta", "ocr.gamma")
    forged = TAMPERS[name](plan)
    assert forged.plan_hash != plan.plan_hash
    with pytest.raises(ExecutionRefused, match="^PLAN_NOT_REPRODUCIBLE$"):
        fab.executor().execute(forged, task)
    assert no_adapter_ran(fab)
    assert fab.executor().execute(plan, task).status is ExecutionStatus.COMPLETED  # the genuine plan runs


def test_cf01_plan_from_another_planner_configuration_is_refused(tmp_path: Path) -> None:
    fab = fallback_fabric(tmp_path)
    task = fab.task()
    preferred = PlannerConfig(preferences={"DOCUMENT_OCR": ("ocr.gamma",)})
    plan = fab.planner(config=preferred).plan(task)
    with pytest.raises(ExecutionRefused, match="PLAN_NOT_REPRODUCIBLE"):  # operator config differs
        fab.executor().execute(plan, task)
    assert no_adapter_ran(fab)
    result = fab.executor(config=preferred).execute(plan, task)
    assert result.plan_receipt.stages[0].adapter_id == "ocr.gamma"
    router = forge(fab.planner().plan(task), 0, router_selection_hash="a" * 64)  # a planner cannot route
    with pytest.raises(ExecutionRefused, match="PLAN_NOT_REPRODUCIBLE"):
        fab.executor().execute(router, task)


def test_cf01_time_varying_advice_is_bound_to_the_recorded_set_without_re_asking(tmp_path: Path) -> None:
    fab = fallback_fabric(tmp_path)
    task = fab.task()
    advisor = Advisor({"ocr.gamma": 0.2})
    config = PlannerConfig(advice_enabled=True)
    plan = fab.planner(advisor=advisor, config=config).plan(task)
    assert plan.stages[0].ordering == ("ocr.gamma", "ocr.alpha", "ocr.beta")
    asked = advisor.calls
    advisor.scores = {"ocr.alpha": 0.25, "ocr.gamma": -0.25}  # advice changed since planning
    result = fab.executor(advisor=advisor, config=config).execute(plan, task)
    assert result.status is ExecutionStatus.COMPLETED and advisor.calls == asked  # not re-asked
    assert result.plan_receipt.stages[0].adapter_id == "ocr.gamma"
    with pytest.raises(ExecutionRefused, match="^ADVICE_NOT_ISSUED$"):  # advice disabled at execution
        fab.executor(advisor=advisor).execute(plan, task)


def test_cf01_recorded_advice_cannot_widen_or_exceed_the_bound(tmp_path: Path) -> None:
    fab = fallback_fabric(tmp_path)
    task = fab.task()
    config = PlannerConfig(advice_enabled=True)
    advisor = Advisor({"ocr.gamma": 0.2})
    plan = fab.planner(advisor=advisor, config=config).plan(task)
    stage = plan.stages[0]
    record = stage.advice[0]
    cloud = AdviceRecord("ocr.cloud", 0.25, 3, record.profile_hash, record.explanation_hash, False)
    beyond = dataclasses.replace(record, adjustment=5.0)
    forgeries = {
        "infeasible_candidate": forge(plan, 0, advice=(*stage.advice, cloud)),
        "beyond_bound": forge(plan, 0, advice=(beyond,)),
        "other_context": forge(plan, 0, advice_context_hash="b" * 64),
        "status_flip": forge(plan, 0, advice_status="ADVICE_ERROR", advice=(),
                             ordering=("ocr.alpha", "ocr.beta", "ocr.gamma"),
                             selected_adapter_id="ocr.alpha"),
    }
    for name, forged in forgeries.items():
        with pytest.raises(ExecutionRefused, match="^ADVICE_NOT_ISSUED$"):
            fab.executor(advisor=advisor, config=config).execute(forged, task)
        assert no_adapter_ran(fab), name


# --- CF-02: trusted input provenance -------------------------------------------------------------


class MutableProvenance:
    def __init__(self, entries: dict[str, Any]) -> None:
        self.entries = entries

    def resolve(self, sha256: str) -> Any:
        return self.entries.get(sha256)


def test_cf02_planner_resolves_inputs_and_never_trusts_labels(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    confidential = DataClassification.CONFIDENTIAL

    def planned(provenance: Any, label: DataClassification = confidential, project: str = PROJECT) -> tuple[str, ...]:
        task = fab.task(inputs=(TaskInput(name="scan", ref=fab.scan, project_id=project, classification=label),))
        return fab.planner(provenance=provenance).plan(task).infeasible_codes

    assert planned(fab.provenance) == ()
    assert planned(None) == ("INPUT_PROVENANCE_RESOLVER_MISSING",)
    assert planned(InMemoryInputProvenance()) == ("INPUT_PROVENANCE_UNKNOWN",)
    assert planned(MutableProvenance({fab.scan.sha256: ("proj-b", confidential)})) == (
        "INPUT_PROVENANCE_PROJECT_MISMATCH",)
    restricted = MutableProvenance({fab.scan.sha256: (PROJECT, DataClassification.RESTRICTED)})
    assert planned(restricted) == ("INPUT_CLASSIFICATION_EXCEEDS_TASK",)
    # A label LOWER than the resolved classification is refused (and so is a higher one).
    assert planned(fab.provenance, label=DataClassification.INTERNAL) == ("INPUT_CLASSIFICATION_LABEL_MISMATCH",)
    internal = MutableProvenance({fab.scan.sha256: (PROJECT, DataClassification.INTERNAL)})
    assert planned(internal) == ("INPUT_CLASSIFICATION_LABEL_MISMATCH",)

    class Broken:
        def resolve(self, sha256: str) -> Any:
            raise RuntimeError("lineage store down")

    assert planned(Broken()) == ("INPUT_PROVENANCE_UNAVAILABLE",)
    assert planned(MutableProvenance({fab.scan.sha256: ("proj-a", "CONFIDENTIAL")})) == ("INPUT_PROVENANCE_INVALID",)
    assert planned(MutableProvenance({fab.scan.sha256: ("proj-a",)})) == ("INPUT_PROVENANCE_INVALID",)


def test_cf02_executor_refuses_without_resolver_and_on_changed_provenance(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    with pytest.raises(ExecutionRefused, match="INPUT_PROVENANCE_RESOLVER_MISSING"):
        fab.executor(provenance=None).execute(plan, task)
    entries: dict[str, Any] = {fab.scan.sha256: (PROJECT, DataClassification.CONFIDENTIAL)}
    live = MutableProvenance(entries)
    entries[fab.scan.sha256] = (PROJECT, DataClassification.RESTRICTED)
    with pytest.raises(ExecutionRefused, match="INPUT_CLASSIFICATION_EXCEEDS_TASK"):
        fab.executor(provenance=live).execute(plan, task)
    entries[fab.scan.sha256] = ("proj-b", DataClassification.CONFIDENTIAL)
    with pytest.raises(ExecutionRefused, match="INPUT_PROVENANCE_PROJECT_MISMATCH"):
        fab.executor(provenance=live).execute(plan, task)
    del entries[fab.scan.sha256]
    with pytest.raises(ExecutionRefused, match="INPUT_PROVENANCE_UNKNOWN"):
        fab.executor(provenance=live).execute(plan, task)
    assert no_adapter_ran(fab)


def test_cf02_provenance_is_rechecked_immediately_before_the_read(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    fab = fabric(tmp_path)
    task = fab.task()
    entries: dict[str, Any] = {fab.scan.sha256: (PROJECT, DataClassification.CONFIDENTIAL)}
    live = MutableProvenance(entries)
    plan = fab.planner(provenance=live).plan(task)
    after_prepare(monkeypatch, "ocr", lambda: entries.update(
        {fab.scan.sha256: (PROJECT, DataClassification.RESTRICTED)}))
    result = fab.executor(provenance=live).execute(plan, task)
    assert result.failure_code == "ocr:INPUT_CLASSIFICATION_EXCEEDS_TASK"
    assert no_adapter_ran(fab)


def test_cf02_in_memory_provenance_is_bounded_and_immutable(tmp_path: Path) -> None:
    store = InMemoryInputProvenance()
    store.register("a" * 64, project_id=PROJECT, classification=DataClassification.INTERNAL)
    store.register("a" * 64, project_id=PROJECT, classification=DataClassification.INTERNAL)  # idempotent
    with pytest.raises(ProvenanceRefused, match="INPUT_PROVENANCE_ENTRY_CONFLICT"):
        store.register("a" * 64, project_id=PROJECT, classification=DataClassification.PUBLIC)
    with pytest.raises(ProvenanceRefused, match="INPUT_PROVENANCE_ENTRY_INVALID"):
        store.register("not-a-hash", project_id=PROJECT, classification=DataClassification.PUBLIC)
    assert store.resolve("a" * 64) == (PROJECT, DataClassification.INTERNAL) and store.resolve("b" * 64) is None


# --- CF-03: authoritative recheck immediately before every invocation ----------------------------


def _inventory_fabric(tmp_path: Path) -> tuple[Fabric, Inventory]:
    return fabric(tmp_path), Inventory()


AFTER_PREPARE: dict[str, tuple[Callable[[Fabric, Inventory], None], str]] = {
    "adapter_lifecycle_revoked": (lambda fab, inv: fab.registry.set_adapter_lifecycle(
        "ocr.alpha", LifecycleState.REVOKED, operator=OPERATOR, reason_code="SECURITY_ADVISORY"),
        "ADAPTER_DESCRIPTOR_CHANGED_SINCE_BINDING"),
    "capability_deprecated": (lambda fab, inv: fab.registry.set_capability_lifecycle(
        "DOCUMENT_OCR", LifecycleState.DEPRECATED, operator=OPERATOR, reason_code="SUPERSEDED"),
        "LIFECYCLE_CHANGED_BEFORE_EXECUTION"),
    "descriptor_swapped": (lambda fab, inv: setattr(fab.adapters["ocr.alpha"], "descriptor", adapter_descriptor(
        "ocr.alpha", "DOCUMENT_OCR", max_classification=DataClassification.PUBLIC)),
        "ADAPTER_DESCRIPTOR_CHANGED_SINCE_INSTALL"),
    "grant_revoked": (lambda fab, inv: fab.grants.grant("TEXT_CHUNK"), "AUTHORIZATION_REVOKED_BEFORE_EXECUTION"),
    "resource_gone": (lambda fab, inv: inv.fits.update({"local-cpu": False}), "RESOURCE_NO_LONGER_FITS"),
}


@pytest.mark.parametrize("name", sorted(AFTER_PREPARE))
def test_cf03_change_between_prepare_and_invoke_blocks_the_adapter(tmp_path: Path, name: str,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    fab, inventory = _inventory_fabric(tmp_path)
    task = fab.task()
    plan = fab.planner(inventory=inventory).plan(task)
    change, code = AFTER_PREPARE[name]
    after_prepare(monkeypatch, "ocr", lambda: change(fab, inventory))
    result = fab.executor(inventory=inventory).execute(plan, task)
    assert result.failure_code == f"ocr:{code}"
    assert not fab.adapters["ocr.alpha"].calls and not fab.adapters["chunk.fixed"].calls


INSIDE_PROBE: dict[str, tuple[Callable[[Fabric, Inventory], None], str]] = {
    # A registry mutation from the dispatching thread itself is refused outright (CF-03, flag not reentrancy).
    "adapter_lifecycle_revoked": (AFTER_PREPARE["adapter_lifecycle_revoked"][0], "REGISTRY_MUTATION_DURING_DISPATCH"),
    "capability_deprecated": (AFTER_PREPARE["capability_deprecated"][0], "REGISTRY_MUTATION_DURING_DISPATCH"),
    "descriptor_swapped": AFTER_PREPARE["descriptor_swapped"],
    "grant_revoked": AFTER_PREPARE["grant_revoked"],
    "resource_gone": AFTER_PREPARE["resource_gone"],
}


@pytest.mark.parametrize("name", sorted(INSIDE_PROBE))
def test_cf03_revocation_triggered_from_inside_available_cannot_slip_through(
        tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The guarded path probes readiness FIRST and runs the authoritative recheck after it."""
    fab, inventory = _inventory_fabric(tmp_path)
    task = fab.task()
    plan = fab.planner(inventory=inventory).plan(task)
    impl = fab.adapters["ocr.alpha"]
    change, code = INSIDE_PROBE[name]
    after_prepare(monkeypatch, "ocr", lambda: setattr(impl, "probe_hook", once(lambda: change(fab, inventory))))
    result = fab.executor(inventory=inventory).execute(plan, task)
    assert result.failure_code == f"ocr:{code}"
    assert not impl.calls and not fab.adapters["chunk.fixed"].calls


def test_cf03_router_evidence_is_rechecked_before_the_model_call(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    fab = fabric(tmp_path)
    task = with_model(fab)
    delegate = Delegate()
    plan = fab.planner(inference=delegate).plan(task)
    after_prepare(monkeypatch, "summary", lambda: setattr(delegate, "worker", "claude-remote"))
    result = fab.executor(inference=delegate).execute(plan, task)
    assert result.failure_code == "summary:WORKER_ROUTER_SELECTION_CHANGED"
    assert not fab.adapters["llm.bridge"].calls


REPLAY_REVOCATIONS: dict[str, tuple[Callable[[Fabric], None], str]] = {
    "adapter_revoked": (lambda fab: fab.registry.set_adapter_lifecycle(
        "chunk.fixed", LifecycleState.REVOKED, operator=OPERATOR, reason_code="SECURITY_ADVISORY"),
        "ADAPTER_DESCRIPTOR_CHANGED_SINCE_BINDING"),
    "capability_deprecated": (lambda fab: fab.registry.set_capability_lifecycle(
        "TEXT_CHUNK", LifecycleState.DEPRECATED, operator=OPERATOR, reason_code="SUPERSEDED"),
        "LIFECYCLE_CHANGED_BEFORE_EXECUTION"),
    "grant_revoked": (lambda fab: fab.grants.grant("DOCUMENT_OCR"), "AUTHORIZATION_REVOKED_BEFORE_EXECUTION"),
}


@pytest.mark.parametrize("name", sorted(REPLAY_REVOCATIONS))
def test_cf03_verification_replay_is_refused_after_revocation(tmp_path: Path, name: str,
                                                               monkeypatch: pytest.MonkeyPatch) -> None:
    fab = fabric(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    change, code = REPLAY_REVOCATIONS[name]
    chunk = fab.adapters["chunk.fixed"]
    original = VerificationEngine.verify

    def verify(self: Any, capability: Any, *args: Any, **kwargs: Any) -> Any:
        if capability.capability_id == "TEXT_CHUNK":
            change(fab)  # revoked after the first (real) run passed its post-check, before the replay
        return original(self, capability, *args, **kwargs)

    monkeypatch.setattr(VerificationEngine, "verify", verify)
    result = fab.executor().execute(plan, task)
    receipt = result.plan_receipt.stages[1]
    assert result.failure_code == f"chunk:{code}" and receipt.outputs == ()
    assert receipt.verification.value == "FAILED"
    assert len(chunk.calls) == 1  # the replay never reached the adapter body


REVOKED_INSIDE_BODY: dict[str, tuple[Callable[[Fabric], None], str]] = {
    "adapter_revoked": (REPLAY_REVOCATIONS["adapter_revoked"][0], "REGISTRY_MUTATION_DURING_DISPATCH"),
    "capability_deprecated": (REPLAY_REVOCATIONS["capability_deprecated"][0], "REGISTRY_MUTATION_DURING_DISPATCH"),
    "grant_revoked": (REPLAY_REVOCATIONS["grant_revoked"][0], "REVOKED_DURING_EXECUTION"),
}


@pytest.mark.parametrize("name", sorted(REVOKED_INSIDE_BODY))
@pytest.mark.parametrize("call", ["first_run", "replay"])
def test_cf03_change_inside_the_adapter_body_withholds_outputs_of_run_and_replay(tmp_path: Path, name: str,
                                                                                  call: str) -> None:
    fab = fabric(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    change, code = REVOKED_INSIDE_BODY[name]
    chunk = fab.adapters["chunk.fixed"]
    calls: list[int] = []

    def hook() -> None:
        calls.append(1)
        if len(calls) == (1 if call == "first_run" else 2):
            change(fab)

    chunk.hook = hook
    result = fab.executor().execute(plan, task)
    receipt = result.plan_receipt.stages[1]
    assert result.failure_code == f"chunk:{code}" and receipt.outputs == ()
    assert result.outputs.get("chunk") is None
    assert len(chunk.calls) == (1 if call == "first_run" else 2)
    if code == "REGISTRY_MUTATION_DURING_DISPATCH":  # the mutation itself never happened
        assert fab.registry.adapter("chunk.fixed").recorded_descriptor().lifecycle is LifecycleState.ACTIVE
        assert fab.registry.capability("TEXT_CHUNK").lifecycle is LifecycleState.ACTIVE


def test_cf03_registered_adapter_has_no_unguarded_execute(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    facade = fab.registry.adapter("ocr.alpha")
    assert not hasattr(facade, "execute")
    checks: list[bool] = []
    guarded = facade.guarded(lambda ready: checks.append(ready), post_check=lambda ready: checks.append(ready))
    assert guarded.descriptor == facade.descriptor and checks == []
    with pytest.raises(TypeError):
        facade.guarded(lambda ready: None)  # type: ignore[call-arg]  # the post-execution check is mandatory


# --- CF-04: verified human decisions --------------------------------------------------------------

KEY_NAME = "PADOS_TEST_CAPABILITY_DECISION_KEY"
KEY_REF = f"secretref://env/{KEY_NAME}"
APPROVER = "jimmy.operator"


def _key() -> str:
    return "operator-held-" + "k" * 40  # assembled at runtime; >= MIN_KEY_BYTES


def _boundary(key: str | None = None) -> SecretBoundary:
    return SecretBoundary(EnvironmentSecretResolver({KEY_NAME: _key() if key is None else key}))


def verifier(tmp_path: Path, *, key: str | None = None, environ: dict[str, str] | None = None,
             operator_keys: dict[str, str] | None = None, ledger: Any = None) -> HmacHumanDecisionVerifier:
    """A verifier over the durable ledger file under ``tmp_path`` (every instance shares that file)."""
    return HmacHumanDecisionVerifier(
        operator_keys=operator_keys or {APPROVER: KEY_REF}, boundary=_boundary(key),
        ledger=ledger if ledger is not None else FileConsumedDecisionLedger(tmp_path / "decisions" / "consumed.jsonl"),
        environ=environ or {})


def issuer(*, key: str | None = None, approver: str = APPROVER, environ: dict[str, str] | None = None
           ) -> Approver:
    """Opened from the test process's own OS account with an injected interactive terminal (CR13b-01)."""
    return open_approver(approver, KEY_REF, _boundary(key), environ=environ)


ISSUED = datetime(2026, 10, 2, 23, 0, tzinfo=UTC)  # the fabric clock starts at 2026-10-03T00:00Z


def approve(task_id: str, plan_hash: str, *, by: Approver | None = None,
            issued_at: datetime = ISSUED, lifetime: timedelta = timedelta(hours=2)) -> dict[str, str]:
    """The human confirms (types the plan hash prefix and the task id), then the issuer signs."""
    return (by or issuer()).approve(task_id, plan_hash, issued_at=issued_at, lifetime=lifetime)


def resign(record: dict[str, str], *, key: str | None = None, **changes: str) -> dict[str, str]:
    """A holder of the key re-signing a modified record (stdlib HMAC, test only)."""
    updated = {**record, **changes}
    material = (_key() if key is None else key).encode()
    updated["signature"] = hmac.new(material, signed_payload(updated), hashlib.sha256).hexdigest()
    return updated


def gated(tmp_path: Path) -> tuple[Fabric, Any, ExecutionPlan]:
    fab = fabric(tmp_path)
    task = fab.task(human_gate=True)
    return fab, task, fab.planner().plan(task)


def test_cf04_gated_task_fails_closed_without_verifier_or_decision(tmp_path: Path) -> None:
    fab, task, plan = gated(tmp_path)
    with pytest.raises(ExecutionRefused, match="HUMAN_DECISION_VERIFIER_MISSING"):
        fab.executor().execute(plan, task)
    record = approve(task.task_id, plan.plan_hash)
    with pytest.raises(ExecutionRefused, match="HUMAN_DECISION_VERIFIER_MISSING"):
        fab.executor().execute(plan, task, human_decision=record)
    with pytest.raises(ExecutionRefused, match="HUMAN_DECISION_REQUIRED"):
        fab.executor(human_decisions=verifier(tmp_path)).execute(plan, task)
    with pytest.raises(ExecutionRefused, match="HUMAN_DECISION_MALFORMED"):
        fab.executor(human_decisions=verifier(tmp_path)).execute(plan, task, human_decision="approval-42")  # type: ignore[arg-type]
    assert no_adapter_ran(fab)


def test_cf04_valid_decision_runs_once_and_is_only_a_reference(tmp_path: Path) -> None:
    fab, task, plan = gated(tmp_path)
    authority = verifier(tmp_path)
    record = approve(task.task_id, plan.plan_hash)
    result = fab.executor(human_decisions=authority).execute(plan, task, human_decision=record)
    assert result.status is ExecutionStatus.COMPLETED
    receipt = result.plan_receipt
    assert receipt.human_decision_ref == f"human-decision:{record['decision_id']}"
    assert receipt.semantic_authority == "NONE" and result.semantic_authority == "NONE"
    assert not {"accepted", "acceptance", "human_acceptance"} & set(receipt.model_dump())
    assert _key() not in json.dumps(record) and _key() not in json.dumps(receipt.model_dump(mode="json"))
    calls = len(fab.adapters["ocr.alpha"].calls)
    with pytest.raises(ExecutionRefused, match="HUMAN_DECISION_ALREADY_CONSUMED"):
        fab.executor(human_decisions=authority).execute(plan, task, human_decision=record)
    assert len(fab.adapters["ocr.alpha"].calls) == calls


def test_cf04_syntactically_valid_but_unverified_decisions_are_refused(tmp_path: Path) -> None:
    fab, task, plan = gated(tmp_path)
    authority = verifier(tmp_path)
    good = approve(task.task_id, plan.plan_hash)
    other_plan = fab.planner().plan(fab.task(human_gate=True, task_id="task-9"))
    cases: dict[str, tuple[dict[str, str], str]] = {
        "forged_signature": ({**good, "signature": "0" * 64}, "HUMAN_DECISION_SIGNATURE_INVALID"),
        "other_key": (approve(task.task_id, plan.plan_hash, by=issuer(key="another-operator-" + "z" * 40)),
                      "HUMAN_DECISION_SIGNATURE_INVALID"),
        "other_plan": (approve(task.task_id, other_plan.plan_hash), "HUMAN_DECISION_PLAN_MISMATCH"),
        "other_task": (approve("task-9", plan.plan_hash), "HUMAN_DECISION_TASK_MISMATCH"),
        "expired": (approve(task.task_id, plan.plan_hash, issued_at=ISSUED - timedelta(hours=3)),
                    "HUMAN_DECISION_EXPIRED"),
        "not_yet_valid": (approve(task.task_id, plan.plan_hash, issued_at=ISSUED + timedelta(hours=5)),
                          "HUMAN_DECISION_NOT_YET_VALID"),
        "lifetime_over_24h": (resign(good, issued_at=(ISSUED - timedelta(hours=1)).isoformat(),
                                     expires_at=(ISSUED + timedelta(hours=26)).isoformat()),
                              "HUMAN_DECISION_LIFETIME_INVALID"),
        "not_affirmative": (resign(good, decision="REJECT"), "HUMAN_DECISION_NOT_AFFIRMATIVE"),
        "automation_origin": (resign(good, origin="AUTOMATION"), "HUMAN_DECISION_AUTOMATION_CONTEXT"),
        "unregistered_operator": (resign(good, operator="someone.else"), "HUMAN_DECISION_OPERATOR_NOT_REGISTERED"),
        "extra_field": ({**good, "accepted": "true"}, "HUMAN_DECISION_MALFORMED"),
        "naive_time": (resign(good, issued_at="2026-10-02T23:00:00"), "HUMAN_DECISION_MALFORMED"),
    }
    for name, (record, code) in cases.items():
        with pytest.raises(ExecutionRefused, match=f"^{code}$"):
            fab.executor(human_decisions=authority).execute(plan, task, human_decision=record)
        assert no_adapter_ran(fab), name
    ci = verifier(tmp_path, environ={"GITHUB_ACTIONS": "true"})
    with pytest.raises(ExecutionRefused, match="HUMAN_DECISION_AUTOMATION_CONTEXT"):
        fab.executor(human_decisions=ci).execute(plan, task, human_decision=good)
    with pytest.raises(HumanDecisionRefused, match="HUMAN_DECISION_AUTOMATION_CONTEXT"):
        issuer(environ={"GITHUB_ACTIONS": "true"})
    with pytest.raises(HumanDecisionRefused, match="HUMAN_DECISION_LIFETIME_INVALID"):
        approve(task.task_id, plan.plan_hash, lifetime=timedelta(hours=25))
    with pytest.raises(ValueError, match="HUMAN_DECISION_OPERATOR_INVALID"):
        verifier(tmp_path, operator_keys={"github-actions": KEY_REF})
    assert no_adapter_ran(fab)
    result = fab.executor(human_decisions=authority).execute(plan, task, human_decision=good)
    assert result.status is ExecutionStatus.COMPLETED  # refusals above did not consume the good decision


def test_cf04_key_problems_and_misbehaving_verifiers_fail_closed(tmp_path: Path) -> None:
    fab, task, plan = gated(tmp_path)
    good = approve(task.task_id, plan.plan_hash)
    for broken in (verifier(tmp_path, key=""), verifier(tmp_path, key="short")):
        with pytest.raises(ExecutionRefused, match="HUMAN_DECISION_KEY_UNAVAILABLE") as caught:
            fab.executor(human_decisions=broken).execute(plan, task, human_decision=good)
        assert _key() not in str(caught.value) and "short" not in str(caught.value)

    class Raising:
        durable_single_use = True

        def verify_and_consume(self, record: Any, **kwargs: Any) -> VerifiedHumanDecision:
            raise RuntimeError("verifier offline")

    class Mismatched:
        durable_single_use = True

        def verify_and_consume(self, record: Any, **kwargs: Any) -> VerifiedHumanDecision:
            return VerifiedHumanDecision("human-decision:x", "task-9", "c" * 64, APPROVER, "2026-10-03T02:00:00")

    with pytest.raises(ExecutionRefused, match="HUMAN_DECISION_VERIFIER_ERROR"):
        fab.executor(human_decisions=Raising()).execute(plan, task, human_decision=good)
    with pytest.raises(ExecutionRefused, match="HUMAN_DECISION_UNVERIFIED"):
        fab.executor(human_decisions=Mismatched()).execute(plan, task, human_decision=good)
    full = FileConsumedDecisionLedger(tmp_path / "full" / "consumed.jsonl", capacity=1)
    full.consume("f" * 32)
    with pytest.raises(ExecutionRefused, match="HUMAN_DECISION_LEDGER_FULL"):
        fab.executor(human_decisions=verifier(tmp_path, ledger=full)).execute(plan, task, human_decision=good)
    assert no_adapter_ran(fab)
    assert HumanDecisionCode.VERIFIED.value == "VERIFIED"


# --- CF-05: registry state and its event commit together ------------------------------------------


def _registry_at_capacity(monkeypatch: pytest.MonkeyPatch, fab: Fabric, free: int) -> None:
    monkeypatch.setattr(registry_module, "MAX_EVENTS", len(fab.registry.events) + free)


def test_cf05_full_event_log_leaves_state_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fab = fabric(tmp_path)
    _registry_at_capacity(monkeypatch, fab, 1)
    fab.registry.set_adapter_lifecycle("ocr.alpha", LifecycleState.DEPRECATED, operator=OPERATOR,
                                       reason_code="SUPERSEDED")  # the last free slot: succeeds
    assert len(fab.registry.events) == registry_module.MAX_EVENTS
    before = len(fab.registry.events)
    operations: dict[str, Callable[[], Any]] = {
        "adapter_lifecycle": lambda: fab.registry.set_adapter_lifecycle(
            "chunk.fixed", LifecycleState.REVOKED, operator=OPERATOR, reason_code="SECURITY_ADVISORY"),
        "capability_lifecycle": lambda: fab.registry.set_capability_lifecycle(
            "TEXT_CHUNK", LifecycleState.REVOKED, operator=OPERATOR, reason_code="SECURITY_ADVISORY"),
        "register_capability": lambda: fab.registry.register_capability(capability("NEW_CAP"), operator=OPERATOR),
        "install": lambda: fab.registry.install(TransformAdapter(adapter_descriptor("ocr.late", "DOCUMENT_OCR")),
                                                operator=OPERATOR),
    }
    for name, operation in operations.items():
        with pytest.raises(RegistryError, match="REGISTRY_EVENT_LOG_FULL"):
            operation()
        assert len(fab.registry.events) == before, name
    assert fab.registry.adapter("chunk.fixed").descriptor.lifecycle is LifecycleState.ACTIVE
    assert fab.registry.capability("TEXT_CHUNK").lifecycle is LifecycleState.ACTIVE
    assert not fab.registry.is_installed("ocr.late")
    with pytest.raises(RegistryError, match="CAPABILITY_NOT_REGISTERED"):
        fab.registry.capability("NEW_CAP")
    assert fab.planner().plan(fab.task()).stages[1].selected_adapter_id == "chunk.fixed"


def test_cf05_register_adapter_at_capacity_does_not_register(tmp_path: Path,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    fab = fabric(tmp_path)
    fab.add(TransformAdapter(adapter_descriptor("ocr.beta", "DOCUMENT_OCR")), register=False)
    _registry_at_capacity(monkeypatch, fab, 0)
    with pytest.raises(RegistryError, match="REGISTRY_EVENT_LOG_FULL"):
        fab.registry.register_adapter("ocr.beta", capability_id="DOCUMENT_OCR", operator=OPERATOR)
    assert not fab.registry.is_registered("ocr.beta")
    assert [a.adapter_id for a in fab.registry.adapters_for("DOCUMENT_OCR")] == ["ocr.alpha"]


def test_cf05_clock_or_minimization_failure_leaves_state_unchanged(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    broken = {"clock": False}

    def clock() -> datetime:
        if broken["clock"]:
            raise OSError("clock unavailable")
        return datetime(2026, 10, 3, tzinfo=UTC)

    registry = CapabilityRegistry(clock=clock)
    registry.register_capability(capability("DOCUMENT_OCR"), operator=OPERATOR)
    events = len(registry.events)
    broken["clock"] = True
    with pytest.raises(RegistryError, match="REGISTRY_CLOCK_UNAVAILABLE"):
        registry.set_capability_lifecycle("DOCUMENT_OCR", LifecycleState.REVOKED, operator=OPERATOR,
                                          reason_code="SECURITY_ADVISORY")
    assert registry.capability("DOCUMENT_OCR").lifecycle is LifecycleState.ACTIVE and len(registry.events) == events
    broken["clock"] = False

    def refuse(record: Any, **kwargs: Any) -> None:
        raise DataMinimizationViolation("PROHIBITED_FIELD", "operator")

    monkeypatch.setattr(registry_module, "assert_minimized", refuse)
    with pytest.raises(DataMinimizationViolation):
        registry.set_capability_lifecycle("DOCUMENT_OCR", LifecycleState.REVOKED, operator=OPERATOR,
                                          reason_code="SECURITY_ADVISORY")
    assert registry.capability("DOCUMENT_OCR").lifecycle is LifecycleState.ACTIVE and len(registry.events) == events
    with pytest.raises(DataMinimizationViolation):
        registry.register_capability(capability("TEXT_CHUNK"), operator=OPERATOR)
    with pytest.raises(RegistryError, match="CAPABILITY_NOT_REGISTERED"):
        registry.capability("TEXT_CHUNK")


def test_plan_status_is_unchanged_by_the_new_seams(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    assert fab.planner().plan(fab.task()).status is PlanStatus.FEASIBLE
