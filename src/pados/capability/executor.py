# SPDX-License-Identifier: AGPL-3.0-only
"""Capability executor: runs a FEASIBLE plan stage by stage and returns chained receipts (never acceptance).

Before anything runs, the executor refuses every plan it cannot reproduce: a plan hash proves content
integrity, not planner provenance, so the executor re-derives the plan with its own planner from the
trusted registry, templates, grants, operator preferences, input provenance and the advice its advisor
reports as ISSUED for each plan binding, and requires the identical plan hash — stages, adapters,
parameters, input bindings, candidates, advice and fallback lists in order. Advice that differs from the
issued set (fabricated, or present without a configured advisor) refuses with ``ADVICE_NOT_ISSUED``; any
other difference with ``PLAN_NOT_REPRODUCIBLE``. Task inputs are resolved through the trusted
``InputProvenance`` seam (the executor never runs without one). A ``human_gate`` task runs only with a
decision confirmed and consumed by the ``HumanDecisionVerifier`` for exactly this task id and plan hash;
that verifier must have no signing capability and must consume through a durable, process-shared ledger.
The receipt records the decision as a reference only.

For every stage, after binding (prepare) the executor re-derives everything the plan relied on and
refuses on any difference:

* the stage scope from the CURRENT grant document of its trusted ``GrantSource`` (``authorize_scope``;
  the same call is the ``revalidate`` passed to ``run_bound_stage``) — never a caller-supplied scope;
* the effective capability / adapter descriptors (operator lifecycle included) and their digests;
* the policy decision (``evaluate``) — its hash must equal the planned decision hash;
* the planned resource (when a ``ResourceInventory`` is configured it must still fit);
* for model inference, a fresh Worker Router selection through the delegate whose evidence hash must equal
  the planned one; the executor keeps that evidence and resolves ``router_selection_hash`` to it.

Then, immediately before EVERY adapter invocation (including fallback alternatives and verification
replays), the guarded registry path runs the adapter-controlled steps (readiness probe, descriptor
integrity) first and the executor's authoritative recheck last: current recorded descriptors and their
digests vs the bound ones, operator lifecycle, the grant, resource fit, ``evaluate`` (decision hash must
equal the bound one), Worker Router evidence for model stages and trusted input provenance. Any
difference refuses with a closed code and the adapter body never runs. The recheck and the adapter call
run under the registry dispatch lock, which every registry mutator also takes: a registry revocation
completes either before the recheck (refused) or after the call returned. The external seams are re-read
inside that lock; each must return a consistent snapshot (operator responsibility).

Post-execution revalidation (CF-03): immediately after the adapter call returns, still under the dispatch
lock and before any output is accepted or verified, the same authoritative recheck runs again (grant,
lifecycle, recorded descriptors and digests, resource fit, ``evaluate`` decision hash, input provenance,
Worker Router evidence; plus the implementation's descriptor integrity). Any change refuses the stage with
``REVOKED_DURING_EXECUTION`` and ALL outputs of that call are withheld: the receipt records the refusal and
references no output. Fallback alternatives and verification replays go through the same guarded path, so
the same holds for them. This post-execution check is the fabric's linearization point: a revocation from
any authority that completes before it prevents the outputs' use; one completing after it is ordered after
acceptance. A registry mutation attempted from the dispatching thread itself (adapter code, a seam callback)
is refused (``REGISTRY_MUTATION_DURING_DISPATCH``) and refuses the dispatch.

``permitted_inputs`` are the validated TaskContract input refs plus outputs written by earlier stages of
THIS run. Failures fail closed (no output refs) or, for PARTIAL_WITH_EVIDENCE capabilities, keep
explicitly partial outputs; downstream stages are then recorded as not executed. Fallback happens only
through ``fallback_permitted`` and the transition hash is recorded. Receipts are evidence for the
existing ExecutionReceipt -> validation -> CAS path; human acceptance is never set here.

Optional trusted ledger (``ledger=``, a ``FileExecutionLedger``): every execution that produced a
``PlanReceipt`` (COMPLETED or FAILED) is appended through ``execution_ledger.append_execution`` - this
module is its only caller - before the result is returned. A failed append refuses with the ledger's
closed code, so no result leaves the executor unrecorded. Without ``ledger=`` behaviour is unchanged.

Dispatch context for verification hooks (CR-16b H-06): while a stage's outputs are verified, the executor
itself publishes ``DispatchContext`` (the validated task it is executing, the plan hash, the stage id, the
capability and the stage's actual bound inputs) through ``current_dispatch()``. Hooks such as a
REFERENCE_CHECK read the invocation from there, never from caller-supplied scope; outside verification it
is ``None``. Adapter invocations (the stage call and the verification replay) run in a copy of the
context, so adapter code cannot change what later hooks see or leave a context behind. Inside one Python process this is not an isolation boundary: code that sets the private
context variable itself is not stopped.
"""
from __future__ import annotations

import contextvars
import re
import uuid
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pados.capability.adapter import (
    CapabilityAdapter,
    CapabilityAdapterError,
    StageBinding,
    StageBindingError,
    StageOutput,
    StageRequest,
    bind_stage,
    run_bound_stage,
)
from pados.capability.artifacts import ArtifactAccessDenied, ArtifactRef, ArtifactStore
from pados.capability.execution_ledger import (
    ExecutionLedgerError,
    FileExecutionLedger,
    append_execution,
    stage_bindings_hash,
)
from pados.capability.graph import InputSource, TemplateRegistry
from pados.capability.human_decision import (
    HumanDecisionRefused,
    HumanDecisionVerifier,
    VerifiedHumanDecision,
    signing_capable,
)
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    FailureSemantics,
    VerificationMethod,
    descriptor_digest,
)
from pados.capability.planner import (
    UNASSIGNED_RESOURCE,
    CapabilityPlanner,
    ExecutionPlan,
    GrantSource,
    PlannedStage,
    PlannerConfig,
    authorize_stage,
    plan_hash_of,
)
from pados.capability.policy import AuthorizedScope, PolicyContext, PolicyDecision, evaluate, fallback_permitted
from pados.capability.provenance import InputProvenance, ProvenanceRefused, check_task_input, check_task_inputs
from pados.capability.receipts import PlanReceipt, StageReceipt, VerificationStatus, chain, parameters_hash
from pados.capability.registry import (
    REVOKED_DURING_EXECUTION,
    AdapterIntegrityError,
    CapabilityRegistry,
    DispatchRefused,
    RegisteredAdapter,
    RegistryError,
)
from pados.capability.seams import (
    InferenceRequest,
    InferenceSelection,
    ModelInferenceDelegate,
    ResourceInventory,
    ScoreAdvisor,
)
from pados.capability.task import QualityRequirement, TaskContract, validated_task
from pados.capability.verification import VerificationEngine, VerificationResult

_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_RUN_ID = re.compile(r"^[a-z0-9]{8,32}$")
_DECISION_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_GENESIS = "0" * 64  # placeholder; ``chain`` links every receipt from the plan genesis
NOT_EXECUTED = "NOT_EXECUTED_UPSTREAM_FAILED"


class ExecutionStatus(StrEnum):
    COMPLETED = "COMPLETED"  # every stage ran and produced outputs: execution complete != accepted
    FAILED = "FAILED"


class ExecutionRefused(PermissionError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _StageRefused(Exception):
    def __init__(self, code: str, decision_hash: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.decision_hash = decision_hash


@dataclass(frozen=True)
class AttemptRecord:
    stage_id: str
    adapter_id: str
    failure_code: str | None


@dataclass(frozen=True)
class ExecutionResult:
    status: ExecutionStatus
    plan_receipt: PlanReceipt
    failure_code: str | None
    outputs: Mapping[str, tuple[ArtifactRef, ...]]
    attempts: tuple[AttemptRecord, ...]
    router_evidence: Mapping[str, InferenceSelection]
    semantic_authority: str = "NONE"


@dataclass(frozen=True)
class _Prepared:
    capability: CapabilityDescriptor
    adapter: RegisteredAdapter
    descriptor: AdapterDescriptor
    decision: PolicyDecision
    binding: StageBinding
    revalidate: Callable[[], AuthorizedScope]
    invoker: CapabilityAdapter  # the guarded registry path: probe -> integrity -> authoritative recheck -> execute


@dataclass(frozen=True)
class DispatchContext:
    """What the executor is verifying right now: set by the executor only, read by verification hooks."""

    task: TaskContract
    plan_hash: str
    stage_id: str
    capability_id: str
    inputs: tuple[ArtifactRef, ...]


_DISPATCH: ContextVar[DispatchContext | None] = ContextVar("pados_capability_dispatch", default=None)


def current_dispatch() -> DispatchContext | None:
    """The executing task and the stage's bound inputs while its outputs are verified; otherwise ``None``."""
    return _DISPATCH.get()


def _code(value: object, default: str) -> str:
    text = str(value)
    return text if _CODE.fullmatch(text) else default


class CapabilityExecutor:
    """``templates``, ``planner_config``, ``advisor``, ``inventory``, ``inference`` and ``provenance`` are the
    trusted state the executor re-derives every plan from (the same objects the planner was built with)."""

    def __init__(self, registry: CapabilityRegistry, store: ArtifactStore, grants: GrantSource, *,
                 templates: TemplateRegistry, provenance: InputProvenance | None = None,
                 planner_config: PlannerConfig | None = None, advisor: ScoreAdvisor | None = None,
                 human_decisions: HumanDecisionVerifier | None = None,
                 verifier: VerificationEngine | None = None, inventory: ResourceInventory | None = None,
                 inference: ModelInferenceDelegate | None = None, clock: Callable[[], datetime] | None = None,
                 run_ids: Callable[[], str] | None = None, max_outputs: int = 32,
                 max_output_bytes: int = 64 * 1024 * 1024, ledger: FileExecutionLedger | None = None) -> None:
        if human_decisions is not None and signing_capable(human_decisions):
            raise ValueError("HUMAN_DECISION_VERIFIER_SIGNING_CAPABLE")
        if ledger is not None and not isinstance(ledger, FileExecutionLedger):
            raise ValueError("EXECUTION_LEDGER_INVALID")
        self._ledger = ledger
        self._registry = registry
        self._store = store
        self._grants = grants
        self._provenance = provenance
        self._human_decisions = human_decisions
        self._verifier = verifier or VerificationEngine()
        self._inventory = inventory
        self._inference = inference
        self._clock = clock or (lambda: datetime.now(UTC))
        self._run_ids = run_ids or (lambda: uuid.uuid4().hex[:16])
        self._max_outputs = max_outputs
        self._max_output_bytes = max_output_bytes
        self._planner = CapabilityPlanner(registry, templates, grants, config=planner_config, inventory=inventory,
                                          advisor=advisor, inference=inference, provenance=provenance)

    def execute(self, plan: ExecutionPlan, task: TaskContract, *,
                human_decision: Mapping[str, Any] | None = None) -> ExecutionResult:
        task = validated_task(task)
        if plan_hash_of(plan) != plan.plan_hash or plan.plan_id != f"plan-{plan.plan_hash[:24]}":
            raise ExecutionRefused("PLAN_HASH_MISMATCH")
        if not plan.feasible or not plan.stages or any(not _complete(stage) for stage in plan.stages):
            raise ExecutionRefused("PLAN_NOT_FEASIBLE")
        if plan.task_hash != task.task_hash:
            raise ExecutionRefused("PLAN_TASK_MISMATCH")
        if self._provenance is None:
            raise ExecutionRefused("INPUT_PROVENANCE_RESOLVER_MISSING")
        try:
            check_task_inputs(task, self._provenance)
        except ProvenanceRefused as exc:
            raise ExecutionRefused(exc.code) from exc
        trusted = self._planner.reproduce(task, plan)
        if _advice_not_issued(trusted, plan):
            raise ExecutionRefused("ADVICE_NOT_ISSUED")  # the plan's advice is not what the advisor issued
        if (trusted.plan_hash != plan.plan_hash or trusted.plan_id != plan.plan_id or not trusted.feasible
                or any(not _complete(stage) for stage in trusted.stages)):
            raise ExecutionRefused("PLAN_NOT_REPRODUCIBLE")
        plan = trusted  # from here on only the re-derived plan is used
        run_id = self._run_ids()
        if not _RUN_ID.fullmatch(run_id):
            raise ExecutionRefused("RUN_ID_INVALID")
        human_decision_ref = self._human_decision(plan, task, human_decision)
        run = _Run(self, plan, task, run_id)
        receipts: list[StageReceipt] = []
        failure: str | None = None
        for stage in plan.stages:
            if failure is not None:
                receipts.append(run.skipped(stage))
                continue
            receipt = run.stage(stage)
            receipts.append(receipt)
            if receipt.failure_code is not None:
                failure = f"{stage.stage_id}:{receipt.failure_code}"
            else:
                run.produced[stage.stage_id] = receipt.outputs
                run.permitted.update(ref.sha256 for ref in receipt.outputs)
        linked = chain(plan.plan_id, plan.plan_hash, receipts)
        plan_receipt = PlanReceipt(plan_id=plan.plan_id, plan_hash=plan.plan_hash,
                                   expected_stage_ids=plan.stage_ids, stages=tuple(linked),
                                   human_decision_ref=human_decision_ref)
        status = ExecutionStatus.FAILED if failure else ExecutionStatus.COMPLETED
        self._record(plan, task, plan_receipt, status)
        return ExecutionResult(status, plan_receipt, failure, MappingProxyType(dict(run.produced)),
                               tuple(run.attempts), MappingProxyType(dict(run.evidence)))

    def _record(self, plan: ExecutionPlan, task: TaskContract, receipt: PlanReceipt, status: ExecutionStatus) -> None:
        """Append the execution to the trusted ledger (when configured); a failed append refuses the result."""
        if self._ledger is None:
            return
        bindings = stage_bindings_hash(plan.template_id, plan.template_hash,
                                       [(stage.stage_id, stage.capability_id, stage.inputs) for stage in plan.stages])
        try:
            append_execution(self._ledger, receipt=receipt, task_id=task.task_id, task_type=task.task_type,
                             task_hash=task.task_hash, bindings_hash=bindings, status=status.value,
                             recorded_at=self._clock())
        except ExecutionLedgerError as exc:
            raise ExecutionRefused(_code(exc.code, "EXECUTION_LEDGER_UNAVAILABLE")) from exc

    def _human_decision(self, plan: ExecutionPlan, task: TaskContract,
                        record: Mapping[str, Any] | None) -> str | None:
        """Verify (and consume) a human decision for exactly this task and plan; returns its reference."""
        if record is None:
            if task.human_gate:
                raise ExecutionRefused("HUMAN_DECISION_VERIFIER_MISSING" if self._human_decisions is None
                                       else "HUMAN_DECISION_REQUIRED")
            return None
        verifier = self._human_decisions
        if verifier is None:
            raise ExecutionRefused("HUMAN_DECISION_VERIFIER_MISSING")
        if signing_capable(verifier):  # the execution side never holds a signing path
            raise ExecutionRefused("HUMAN_DECISION_VERIFIER_SIGNING_CAPABLE")
        try:
            durable = verifier.durable_single_use is True
        except Exception:  # noqa: BLE001 - a verifier that cannot say it is durable is not durable
            durable = False
        if not durable:  # single use must survive restarts and be shared across processes
            raise ExecutionRefused("HUMAN_DECISION_LEDGER_NOT_DURABLE")
        if not isinstance(record, Mapping):
            raise ExecutionRefused("HUMAN_DECISION_MALFORMED")
        try:
            verified = verifier.verify_and_consume(record, task_id=task.task_id, plan_hash=plan.plan_hash,
                                                   now=self._clock())
        except HumanDecisionRefused as exc:
            raise ExecutionRefused(_code(exc.code, "HUMAN_DECISION_REFUSED")) from exc
        except Exception as exc:  # noqa: BLE001 - a verifier that cannot answer never authorizes
            raise ExecutionRefused("HUMAN_DECISION_VERIFIER_ERROR") from exc
        if not (isinstance(verified, VerifiedHumanDecision) and verified.task_id == task.task_id
                and verified.plan_hash == plan.plan_hash and isinstance(verified.decision_ref, str)
                and _DECISION_REF.fullmatch(verified.decision_ref)):
            raise ExecutionRefused("HUMAN_DECISION_UNVERIFIED")
        return verified.decision_ref


class _Run:
    """State of one execution of one plan (permitted inputs, produced outputs, router evidence)."""

    def __init__(self, executor: CapabilityExecutor, plan: ExecutionPlan, task: TaskContract, run_id: str) -> None:
        self.x = executor
        self.plan = plan
        self.task = task
        self.run_id = run_id
        self.permitted: set[str] = {item.ref.sha256 for item in task.inputs}
        self.produced: dict[str, tuple[ArtifactRef, ...]] = {}
        self.evidence: dict[str, InferenceSelection] = {}
        self.attempts: list[AttemptRecord] = []

    # --- one stage ---------------------------------------------------------------------------

    def stage(self, stage: PlannedStage) -> StageReceipt:
        started = self.x._clock()
        adapter_id, decision_hash = str(stage.selected_adapter_id), str(stage.decision_hash)  # _complete()
        try:
            prepared = self.prepare(stage, adapter_id, stage.resource_id, decision_hash)
        except _StageRefused as exc:
            self.attempts.append(AttemptRecord(stage.stage_id, adapter_id, exc.code))
            return self.planned_receipt(stage, started, exc.code, exc.decision_hash, attempt=1)
        output, failure = self.run(stage, prepared)
        transition_hash: str | None = None
        attempt = 1
        if failure is not None and failure.startswith("ADAPTER:"):
            for alternative in stage.alternatives:
                attempt += 1
                fallback = self.fallback(stage, prepared, alternative)
                if fallback is None:
                    continue
                alt_prepared, transition_hash = fallback
                prepared = alt_prepared
                output, failure = self.run(stage, prepared)
                if failure is None or not failure.startswith("ADAPTER:"):
                    break
        if failure is not None or output is None:
            code = (failure or "ADAPTER_OUTPUT_MISSING").removeprefix("ADAPTER:")
            return self.receipt(stage, prepared, started, (), code, None, transition_hash, attempt)
        return self.completed(stage, prepared, output, started, transition_hash, attempt)

    def prepare(self, stage: PlannedStage, adapter_id: str, resource_id: str, expected_decision: str) -> _Prepared:
        registry, task = self.x._registry, self.task
        try:
            capability = registry.capability(stage.capability_id)
            adapter = registry.adapter(adapter_id)
            descriptor = adapter.descriptor
        except (RegistryError, AdapterIntegrityError) as exc:
            raise _StageRefused(_code(exc, "REGISTRY_REFUSED")) from exc
        try:
            scope = authorize_stage(task, stage.capability_id, self.x._grants)
        except PermissionError as exc:
            raise _StageRefused("AUTHORIZATION_REVOKED_BEFORE_EXECUTION") from exc
        if not self.resource_still_fits(descriptor, resource_id):
            raise _StageRefused("RESOURCE_NO_LONGER_FITS")
        context = PolicyContext(scope=scope, project_id=task.project_id, stage_id=stage.stage_id,
                                classification=task.classification, network_policy=task.network_policy,
                                resource_id=resource_id)
        decision = evaluate(capability, descriptor, context, adapter_available=adapter.available)
        if not decision.allowed:
            raise _StageRefused(f"POLICY_{decision.code.value}", decision.decision_hash)
        if descriptor_digest(capability) != stage.capability_digest:
            raise _StageRefused("CAPABILITY_CHANGED_SINCE_PLANNING", decision.decision_hash)
        if decision.decision_hash != expected_decision:
            raise _StageRefused("POLICY_DECISION_CHANGED_SINCE_PLANNING", decision.decision_hash)
        router_hash = self.router_selection(stage, capability, scope) if (
            capability.family is CapabilityFamily.MODEL_RUNTIME) else None
        request = StageRequest(stage.stage_id, stage.capability_id, self.inputs(stage), stage.parameters,
                               task.classification, stage.seed, router_hash)
        if request.router_selection_hash is not None:
            self.resolve_router_evidence(request.router_selection_hash)
        try:
            binding = bind_stage(capability, descriptor, context, decision, request,
                                 permitted_inputs=frozenset(self.permitted))
        except StageBindingError as exc:
            raise _StageRefused(_code(exc, "STAGE_BINDING_REFUSED"), decision.decision_hash) from exc

        def revalidate() -> AuthorizedScope:
            return authorize_stage(task, stage.capability_id, self.x._grants)

        def final_check(ready: bool) -> None:
            self.recheck(stage, binding, ready)

        def post_check(ready: bool) -> None:
            """Right after the call returned, before its output is accepted or verified (CF-03)."""
            try:
                self.recheck(stage, binding, ready)
            except Exception as exc:  # noqa: BLE001 - any change or unanswerable authority withholds the output
                raise _StageRefused(REVOKED_DURING_EXECUTION) from exc

        return _Prepared(capability, adapter, descriptor, decision, binding, revalidate,
                         adapter.guarded(final_check, post_check=post_check))

    def recheck(self, stage: PlannedStage, binding: StageBinding, ready: bool) -> None:
        """Authoritative policy re-evaluation from CURRENT trusted state; runs no adapter code.

        ``ready`` is the readiness probe result the guarded path obtained just before this call."""
        capability, descriptor = self.recorded_state(stage, binding)
        try:
            scope = authorize_stage(self.task, stage.capability_id, self.x._grants)
        except PermissionError as exc:
            raise _StageRefused("AUTHORIZATION_REVOKED_BEFORE_EXECUTION") from exc
        if scope.grant_hash != binding.context.scope.grant_hash:
            raise _StageRefused("AUTHORIZATION_CHANGED_BEFORE_EXECUTION")
        if not self.resource_still_fits(descriptor, binding.context.resource_id):
            raise _StageRefused("RESOURCE_NO_LONGER_FITS")
        context = PolicyContext(scope=scope, project_id=self.task.project_id, stage_id=stage.stage_id,
                                classification=self.task.classification, network_policy=self.task.network_policy,
                                resource_id=binding.context.resource_id)
        decision = evaluate(capability, descriptor, context, adapter_available=lambda: ready)
        if not decision.allowed:
            raise _StageRefused(f"POLICY_{decision.code.value}", decision.decision_hash)
        if decision.decision_hash != binding.decision.decision_hash:
            raise _StageRefused("POLICY_DECISION_CHANGED_BEFORE_EXECUTION", decision.decision_hash)
        if capability.family is CapabilityFamily.MODEL_RUNTIME and (
                self.router_selection(stage, capability, scope) != binding.request.router_selection_hash):
            raise _StageRefused("WORKER_ROUTER_SELECTION_CHANGED")
        self.check_inputs(binding.request.inputs)
        self.recorded_state(stage, binding)  # the trusted seams above cannot have changed the registry either

    def recorded_state(self, stage: PlannedStage, binding: StageBinding
                       ) -> tuple[CapabilityDescriptor, AdapterDescriptor]:
        registry = self.x._registry
        try:
            capability = registry.capability(stage.capability_id)
            descriptor = registry.adapter(binding.adapter.adapter_id).recorded_descriptor()
        except RegistryError as exc:
            raise _StageRefused(_code(exc, "REGISTRY_REFUSED")) from exc
        if not (capability.lifecycle.plannable and descriptor.lifecycle.plannable):
            raise _StageRefused("LIFECYCLE_CHANGED_BEFORE_EXECUTION")
        if descriptor_digest(capability) != binding.capability_digest:
            raise _StageRefused("CAPABILITY_CHANGED_BEFORE_EXECUTION")
        if descriptor_digest(descriptor) != binding.adapter_digest:
            raise _StageRefused("ADAPTER_DESCRIPTOR_CHANGED_BEFORE_EXECUTION")
        return capability, descriptor

    def check_inputs(self, refs: tuple[ArtifactRef, ...]) -> None:
        """Task inputs: trusted provenance again; anything else: an output of an earlier stage of THIS run."""
        task_inputs = {item.ref.sha256: item for item in self.task.inputs}
        for ref in refs:
            item = task_inputs.get(ref.sha256)
            if item is not None:
                try:
                    check_task_input(self.task, item, self.x._provenance)
                except ProvenanceRefused as exc:
                    raise _StageRefused(exc.code) from exc
            elif ref.sha256 not in self.permitted:
                raise _StageRefused("STAGE_INPUT_NOT_PERMITTED")

    def resource_still_fits(self, descriptor: AdapterDescriptor, resource_id: str) -> bool:
        if self.x._inventory is None:
            return resource_id == UNASSIGNED_RESOURCE
        try:
            matches = self.x._inventory.match(descriptor.resources)
        except Exception:  # noqa: BLE001 - an inventory that cannot answer means "does not fit"
            return False
        return any(match.resource_id == resource_id and match.fits is True for match in matches)

    def router_selection(self, stage: PlannedStage, capability: CapabilityDescriptor, scope: AuthorizedScope) -> str:
        if self.x._inference is None:
            raise _StageRefused("MODEL_INFERENCE_DELEGATE_MISSING")
        selection = self.x._inference.select(InferenceRequest(
            project_id=self.task.project_id, stage_id=stage.stage_id, capability_id=capability.capability_id,
            risk_level=self.task.risk_level.value, classification=self.task.classification,
            network_policy=self.task.network_policy.value, grant_hash=scope.grant_hash))
        if selection.status != "ROUTED" or selection.selected_worker_id is None:
            raise _StageRefused("WORKER_ROUTER_BLOCKED")
        if selection.selection_hash != stage.router_selection_hash:
            raise _StageRefused("WORKER_ROUTER_SELECTION_CHANGED")
        self.evidence[selection.selection_hash] = selection
        return selection.selection_hash

    def resolve_router_evidence(self, selection_hash: str) -> InferenceSelection:
        selection = self.evidence.get(selection_hash)
        if selection is None or selection.status != "ROUTED" or selection.selected_worker_id is None:
            raise _StageRefused("WORKER_ROUTER_EVIDENCE_UNRESOLVED")
        return selection

    def inputs(self, stage: PlannedStage) -> tuple[ArtifactRef, ...]:
        refs: dict[str, ArtifactRef] = {}
        for binding in stage.inputs:
            if binding.source is InputSource.TASK_INPUT:
                try:
                    found: tuple[ArtifactRef, ...] = (self.task.input_ref(binding.name),)
                except KeyError as exc:
                    raise _StageRefused("TASK_INPUT_UNRESOLVED") from exc
            else:
                if binding.name not in self.produced:
                    raise _StageRefused("UPSTREAM_OUTPUT_MISSING")
                found = tuple(ref for ref in self.produced[binding.name]
                              if binding.artifact_kind is None or ref.kind == binding.artifact_kind)
                if not found:
                    raise _StageRefused("UPSTREAM_OUTPUT_KIND_MISSING")
            for ref in found:
                refs.setdefault(ref.sha256, ref)
        return tuple(refs.values())

    def run(self, stage: PlannedStage, prepared: _Prepared) -> tuple[StageOutput | None, str | None]:
        """Returns (output, failure). Failures prefixed ``ADAPTER:`` are adapter errors (fallback-eligible)."""
        try:  # in a copied context: adapter code cannot leave a dispatch context behind
            output = contextvars.copy_context().run(
                run_bound_stage, prepared.binding, prepared.invoker, self.x._store, revalidate=prepared.revalidate,
                max_outputs=self.x._max_outputs, max_output_bytes=self.x._max_output_bytes)
        except _StageRefused as exc:  # the authoritative pre/post-invocation recheck refused: never fallback
            failure = exc.code
        except DispatchRefused as exc:  # e.g. a registry mutation attempted from inside the dispatch
            failure = _code(exc.code, "DISPATCH_REFUSED")
        except CapabilityAdapterError as exc:
            failure = "ADAPTER:" + _code(exc.code, "ADAPTER_FAILED")
        except StageBindingError as exc:
            failure = _code(exc, "STAGE_BINDING_REFUSED")
        except AdapterIntegrityError:
            failure = "ADAPTER_DESCRIPTOR_CHANGED_SINCE_INSTALL"
        except ArtifactAccessDenied:
            failure = "ADAPTER_ARTIFACT_ACCESS_DENIED"
        except Exception:  # noqa: BLE001 - any other adapter exception fails the stage closed
            failure = "ADAPTER_UNEXPECTED_ERROR"
        else:
            self.attempts.append(AttemptRecord(stage.stage_id, prepared.descriptor.adapter_id, None))
            return output, None
        self.attempts.append(AttemptRecord(stage.stage_id, prepared.descriptor.adapter_id,
                                           failure.removeprefix("ADAPTER:")))
        return None, failure

    def fallback(self, stage: PlannedStage, primary: _Prepared, alternative: str) -> tuple[_Prepared, str] | None:
        planned = next((c for c in stage.candidates if c.adapter_id == alternative and c.decision_hash), None)
        if planned is None or planned.decision_hash is None:
            return None
        try:
            prepared = self.prepare(stage, alternative, planned.resource_id, planned.decision_hash)
        except _StageRefused as exc:
            self.attempts.append(AttemptRecord(stage.stage_id, alternative, exc.code))
            return None
        transition = fallback_permitted(primary.capability, primary.descriptor, prepared.descriptor,
                                        primary.decision, prepared.decision)
        if transition is None:
            self.attempts.append(AttemptRecord(stage.stage_id, alternative, "FALLBACK_NOT_PERMITTED"))
            return None
        return prepared, transition.transition_hash

    def completed(self, stage: PlannedStage, prepared: _Prepared, output: StageOutput, started: datetime,
                  transition_hash: str | None, attempt: int) -> StageReceipt:
        if output.partial:
            if prepared.capability.failure is not FailureSemantics.PARTIAL_WITH_EVIDENCE:
                return self.receipt(stage, prepared, started, (), "PARTIAL_OUTPUT_NOT_PERMITTED", None,
                                    transition_hash, attempt, metrics=output.metrics)
            return self.receipt(stage, prepared, started, output.outputs, "PARTIAL_OUTPUT", None, transition_hash,
                                attempt, partial=True, metrics=output.metrics)
        binding, invoker = prepared.binding, prepared.invoker
        refused: list[str] = []

        def replay() -> StageOutput:  # the replay goes through the same guarded path and recheck
            try:  # in a copied context: adapter code cannot change the dispatch context later hooks read
                return contextvars.copy_context().run(
                    run_bound_stage, binding, invoker, self.x._store, revalidate=prepared.revalidate,
                    max_outputs=self.x._max_outputs, max_output_bytes=self.x._max_output_bytes)
            except (_StageRefused, DispatchRefused) as exc:
                refused.append(exc.code)
                raise
            except (StageBindingError, AdapterIntegrityError) as exc:
                refused.append(_code(exc, "ADAPTER_DESCRIPTOR_CHANGED_SINCE_INSTALL"))
                raise

        entered = _DISPATCH.set(DispatchContext(self.task, self.plan.plan_hash, stage.stage_id,
                                              prepared.capability.capability_id, binding.request.inputs))
        try:
            verification = self.x._verifier.verify(prepared.capability, output, self.x._store, seed=stage.seed,
                                                   replay=replay, adapter_determinism=prepared.descriptor.determinism)
        finally:
            _DISPATCH.reset(entered)
        if refused:
            return self.receipt(stage, prepared, started, (), refused[0], verification, transition_hash, attempt,
                                metrics=output.metrics)
        if verification.status is VerificationStatus.FAILED:
            return self.receipt(stage, prepared, started, (), "VERIFICATION_FAILED", verification,
                                transition_hash, attempt, metrics=output.metrics)
        if self.task.quality is QualityRequirement.VERIFIED and verification.status is not VerificationStatus.PASSED:
            return self.receipt(stage, prepared, started, (), "VERIFICATION_REQUIRED_NOT_PASSED", verification,
                                transition_hash, attempt, metrics=output.metrics)
        return self.receipt(stage, prepared, started, output.outputs, None, verification, transition_hash, attempt,
                            metrics=output.metrics)

    # --- receipts ----------------------------------------------------------------------------

    def receipt(self, stage: PlannedStage, prepared: _Prepared, started: datetime,
                outputs: tuple[ArtifactRef, ...], failure: str | None, verification: VerificationResult | None,
                transition_hash: str | None, attempt: int, *, partial: bool = False,
                metrics: Mapping[str, float] | None = None) -> StageReceipt:
        finished = self.x._clock()
        request = prepared.binding.request
        return StageReceipt(
            plan_id=self.plan.plan_id, stage_id=stage.stage_id, attempt_id=self.attempt_id(stage, attempt),
            capability_id=prepared.capability.capability_id, capability_family=prepared.capability.family,
            capability_version=prepared.capability.version, capability_digest=prepared.binding.capability_digest,
            adapter_id=prepared.descriptor.adapter_id, adapter_version=prepared.descriptor.version,
            adapter_digest=prepared.binding.adapter_digest, resource_id=prepared.binding.context.resource_id,
            determinism=prepared.descriptor.determinism, inputs=request.inputs, outputs=outputs,
            parameters_hash=parameters_hash(request.parameters), seed=request.seed,
            started_at=started.isoformat(), finished_at=finished.isoformat(),
            duration_ms=_duration_ms(started, finished),
            verification_method=verification.method if verification else prepared.capability.verification[0],
            verification=verification.status if verification else VerificationStatus.NOT_VERIFIED,
            verification_evidence_hash=verification.evidence_hash if verification else None,
            verifier_id=verification.verifier_id if verification else None,
            policy_decision_hash=prepared.decision.decision_hash, fallback_transition_hash=transition_hash,
            router_selection_hash=request.router_selection_hash, partial=partial, failure_code=failure,
            metrics=dict(metrics or {}), prev_hash=_GENESIS)

    def planned_receipt(self, stage: PlannedStage, started: datetime, failure: str, decision_hash: str | None,
                        *, attempt: int) -> StageReceipt:
        """Receipt for a stage refused before its adapter ran (identities as planned)."""
        if not (stage.capability_family and stage.capability_version and stage.capability_digest
                and stage.selected_adapter_id and stage.adapter_version and stage.adapter_digest
                and stage.determinism and stage.decision_hash):
            raise ExecutionRefused("PLAN_STAGE_INCOMPLETE")
        finished = self.x._clock()
        return StageReceipt(
            plan_id=self.plan.plan_id, stage_id=stage.stage_id, attempt_id=self.attempt_id(stage, attempt),
            capability_id=stage.capability_id, capability_family=stage.capability_family,
            capability_version=stage.capability_version, capability_digest=stage.capability_digest,
            adapter_id=stage.selected_adapter_id, adapter_version=stage.adapter_version,
            adapter_digest=stage.adapter_digest, resource_id=stage.resource_id, determinism=stage.determinism,
            inputs=(), outputs=(), parameters_hash=parameters_hash(stage.parameters), seed=stage.seed,
            started_at=started.isoformat(), finished_at=finished.isoformat(),
            duration_ms=_duration_ms(started, finished), verification_method=VerificationMethod.NONE,
            verification=VerificationStatus.NOT_VERIFIED, policy_decision_hash=decision_hash or stage.decision_hash,
            router_selection_hash=stage.router_selection_hash, failure_code=failure, prev_hash=_GENESIS)

    def skipped(self, stage: PlannedStage) -> StageReceipt:
        return self.planned_receipt(stage, self.x._clock(), NOT_EXECUTED, None, attempt=0)

    def attempt_id(self, stage: PlannedStage, attempt: int) -> str:
        return f"{self.run_id}.{stage.stage_id}.{attempt}"


def _advice_not_issued(trusted: ExecutionPlan, plan: ExecutionPlan) -> bool:
    """True when the plan's advice (per stage both plans share) differs from what the advisor issued."""
    if trusted.advice_enabled != plan.advice_enabled:
        return True
    issued = {stage.stage_id: (stage.advice, stage.advice_status, stage.advice_context_hash)
              for stage in trusted.stages}
    return any(stage.stage_id in issued
               and issued[stage.stage_id] != (stage.advice, stage.advice_status, stage.advice_context_hash)
               for stage in plan.stages)


def _complete(stage: PlannedStage) -> bool:
    return bool(stage.feasible and stage.capability_family and stage.capability_version
                and stage.capability_digest and stage.selected_adapter_id and stage.adapter_version
                and stage.adapter_digest and stage.determinism and stage.decision_hash)


def _duration_ms(started: datetime, finished: datetime) -> int:
    return max(0, int((finished - started).total_seconds() * 1000))
