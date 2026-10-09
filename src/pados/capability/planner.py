# SPDX-License-Identifier: AGPL-3.0-only
"""Capability planner: TaskContract -> capability graph -> one policy-feasible adapter per stage.

Per stage: candidates = REGISTERED adapters of the stage capability -> ``policy.evaluate`` (project grant,
classification, network, lifecycle, availability) -> optional ``ResourceInventory`` fit -> feasible set.
Ordering is the operator's explicit preference list, then adapter_id (deterministic). An optional
``ScoreAdvisor`` is consulted ONLY when ``advice_enabled``; its adjustments are clamped by
``ADVICE_BOUND``, apply only among feasible non-preferred candidates (explicit operator preference is
never overturned), and are recorded in the plan with profile hashes. Advice is bound to the exact
decision: the planner passes the feasible candidate set (sorted ``(adapter_id, version)``), the task
classification and an advice-independent ``plan_binding_hash`` (task hash, template hash, capability,
feasible set) to ``context_hash`` and hands that expected hash to ``apply_advice``; advice bound to anything
else is dropped. With advice disabled the plan is
identical to a plan built without an advisor.

Model-inference stages are resolved by the ``ModelInferenceDelegate`` (the Worker Router bridge); the
planner never ranks language-model providers and never imports provider code. A stage without a feasible
candidate makes the plan INFEASIBLE with every candidate's policy decision; there is no fallback to
another capability, engine family or provider. The plan is content-addressed (``plan_hash``).

Advice provenance (CF-01): the plan never records the advisor's transient return value. After asking once,
the planner reads back what the advisor reports it ISSUED and durably recorded for this exact
``plan_binding_hash`` (``ScoreAdvisor.issued``) and records only that set (clamped, feasible-only,
context-bound as above). Nothing issued -> no advice (``ADVICE_NOT_ISSUED``, or ``ADVICE_ERROR`` when the
advisor raised).

Task inputs are resolved through the trusted ``InputProvenance`` seam before anything is planned (an
unknown artifact, a foreign project, a classification above the task or a differing label makes the plan
INFEASIBLE). ``reproduce`` re-derives a plan from the same trusted state (registry, templates, grants,
operator preferences, and the advisor's ISSUED advice for each binding) for the executor, which refuses
any plan it cannot reproduce bit for bit. Reproduction never re-asks the advisor (advice may vary over
time) and never trusts the advice a plan carries: fabricated advice, even bounded and with recomputed
hashes, differs from the issued set and the plan is refused.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from pados.capability.graph import CapabilityGraph, GraphError, InputBinding, TemplateRegistry
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    FallbackPolicy,
    VerificationMethod,
    descriptor_digest,
)
from pados.capability.policy import AuthorizedScope, PolicyCode, PolicyContext, authorize_scope, evaluate
from pados.capability.provenance import InputProvenance, ProvenanceRefused, check_task_inputs
from pados.capability.registry import AdapterIntegrityError, CapabilityRegistry, RegisteredAdapter, RegistryError
from pados.capability.seams import (
    ADVICE_BOUND,
    Advice,
    AdviceRejected,
    InferenceRequest,
    ModelInferenceDelegate,
    ResourceInventory,
    ResourceMatch,
    ScoreAdvisor,
    apply_advice,
)
from pados.capability.task import QualityRequirement, Scalar, TaskContract, validated_task
from pados.execution.context import canonical_hash

PLAN_SCHEMA = "pados.capability.plan.v1"
UNASSIGNED_RESOURCE = "unassigned"
_AUTOMATIC = frozenset({VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY,
                        VerificationMethod.REFERENCE_CHECK})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RESOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_NOT_ISSUED = frozenset({"ADVICE_ERROR", "ADVICE_NOT_ISSUED"})  # no advice was issued for the binding
MAX_ISSUED_ADVICE = 64
_PREFERRED_TIER_GAP = 1.0  # > 2 * ADVICE_BOUND: advice can never cross an explicit preference
if _PREFERRED_TIER_GAP <= 2 * ADVICE_BOUND:  # pragma: no cover - guards a contract constant change
    raise RuntimeError("ADVICE_BOUND_EXCEEDS_PREFERENCE_TIER")


class PlanStatus(StrEnum):
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"


class GrantSource(Protocol):
    """The trusted source of the CURRENT project grant document (e.g. the governed grant store)."""

    def current_grant(self, *, project_id: str, work_contract_id: str) -> Mapping[str, Any]: ...


def capability_grant(capability_id: str) -> str:
    return f"CAPABILITY:{capability_id}"


def authorize_stage(task: TaskContract, capability_id: str, grants: GrantSource) -> AuthorizedScope:
    """Re-derive the stage scope through the existing grant validator from the CURRENT grant document."""
    try:
        grant = grants.current_grant(project_id=task.project_id, work_contract_id=task.work_contract_id)
    except LookupError as exc:
        raise PermissionError("GRANT_NOT_FOUND") from exc
    return authorize_scope(task.work_contract((capability_grant(capability_id),)), grant)


def _diagnostic_scope(task: TaskContract, grants: GrantSource) -> AuthorizedScope:
    """A validated scope that authorizes nothing (so unauthorized candidates still get explained)."""
    grant = grants.current_grant(project_id=task.project_id, work_contract_id=task.work_contract_id)
    return authorize_scope(task.work_contract(()), grant)


@dataclass(frozen=True)
class PlannerConfig:
    preferences: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # capability_id -> adapter ids
    advice_enabled: bool = False


@dataclass(frozen=True)
class CandidateRecord:
    adapter_id: str
    adapter_digest: str | None
    code: str
    decision_hash: str | None
    resource_id: str
    resource_reasons: tuple[str, ...]
    failing_checks: tuple[str, ...]


@dataclass(frozen=True)
class AdviceRecord:
    adapter_id: str
    adjustment: float
    sample_size: int
    profile_hash: str
    explanation_hash: str
    preferred_tier: bool  # True: recorded but cannot reorder an explicit operator preference


@dataclass(frozen=True)
class PlannedStage:
    stage_id: str
    capability_id: str
    capability_family: CapabilityFamily | None
    capability_version: str | None
    capability_digest: str | None
    inputs: tuple[InputBinding, ...]
    parameters: Mapping[str, Scalar]
    seed: int | None
    candidates: tuple[CandidateRecord, ...]
    ordering: tuple[str, ...]  # feasible adapters, best first
    selected_adapter_id: str | None
    adapter_digest: str | None
    adapter_version: str | None
    determinism: Determinism | None  # of the selected adapter
    decision_hash: str | None
    resource_id: str
    alternatives: tuple[str, ...]  # only when the capability opts into explicit same-policy fallback
    advice: tuple[AdviceRecord, ...]
    advice_status: str | None
    advice_context_hash: str | None
    router_selection_hash: str | None
    router_status: str | None
    infeasible_code: str | None

    @property
    def feasible(self) -> bool:
        return self.infeasible_code is None and self.selected_adapter_id is not None

    def document(self) -> dict[str, Any]:
        return {
            "stage_id": self.stage_id, "capability_id": self.capability_id,
            "capability_family": self.capability_family.value if self.capability_family else None,
            "capability_digest": self.capability_digest, "capability_version": self.capability_version,
            "adapter_version": self.adapter_version,
            "determinism": self.determinism.value if self.determinism else None,
            "inputs": [item.model_dump(mode="json") for item in self.inputs],
            "parameters": dict(self.parameters), "seed": self.seed,
            "candidates": [[c.adapter_id, c.adapter_digest, c.code, c.decision_hash, c.resource_id,
                            list(c.resource_reasons), list(c.failing_checks)] for c in self.candidates],
            "ordering": list(self.ordering), "selected_adapter_id": self.selected_adapter_id,
            "adapter_digest": self.adapter_digest, "decision_hash": self.decision_hash,
            "resource_id": self.resource_id, "alternatives": list(self.alternatives),
            "advice": [[a.adapter_id, a.adjustment, a.sample_size, a.profile_hash, a.explanation_hash,
                        a.preferred_tier] for a in self.advice],
            "advice_status": self.advice_status, "advice_context_hash": self.advice_context_hash,
            "router_selection_hash": self.router_selection_hash, "router_status": self.router_status,
            "infeasible_code": self.infeasible_code,
        }


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    plan_hash: str
    status: PlanStatus
    task_id: str
    task_hash: str
    template_id: str | None
    template_hash: str | None
    graph_hash: str | None
    stages: tuple[PlannedStage, ...]
    infeasible_codes: tuple[str, ...]
    advice_enabled: bool
    human_gate: bool
    quality: QualityRequirement

    @property
    def feasible(self) -> bool:
        return self.status is PlanStatus.FEASIBLE

    @property
    def stage_ids(self) -> tuple[str, ...]:
        return tuple(stage.stage_id for stage in self.stages)

    def document(self) -> dict[str, Any]:
        return plan_document(self.status, self.task_id, self.task_hash, self.template_id, self.template_hash,
                             self.graph_hash, self.stages, self.infeasible_codes, self.advice_enabled,
                             self.human_gate, self.quality)


def plan_document(status: PlanStatus, task_id: str, task_hash: str, template_id: str | None,
                  template_hash: str | None, graph_hash: str | None, stages: Sequence[PlannedStage],
                  infeasible_codes: Sequence[str], advice_enabled: bool, human_gate: bool,
                  quality: QualityRequirement) -> dict[str, Any]:
    return {"schema_version": PLAN_SCHEMA, "status": status.value, "task_id": task_id, "task_hash": task_hash,
            "template_id": template_id, "template_hash": template_hash, "graph_hash": graph_hash,
            "stages": [stage.document() for stage in stages], "infeasible_codes": list(infeasible_codes),
            "advice_enabled": advice_enabled, "human_gate": human_gate, "quality": quality.value}


def plan_hash_of(plan: ExecutionPlan) -> str:
    return canonical_hash(plan.document())


class CapabilityPlanner:
    def __init__(self, registry: CapabilityRegistry, templates: TemplateRegistry, grants: GrantSource, *,
                 config: PlannerConfig | None = None, inventory: ResourceInventory | None = None,
                 advisor: ScoreAdvisor | None = None, inference: ModelInferenceDelegate | None = None,
                 provenance: InputProvenance | None = None) -> None:
        self._registry = registry
        self._templates = templates
        self._grants = grants
        self._config = config or PlannerConfig()
        self._inventory = inventory
        self._advisor = advisor
        self._inference = inference
        self._provenance = provenance

    def plan(self, task: TaskContract) -> ExecutionPlan:
        return self._build(validated_task(task), None)

    def reproduce(self, task: TaskContract, recorded: ExecutionPlan) -> ExecutionPlan:
        """Re-derive ``recorded`` from trusted state; advice comes only from ``advisor.issued`` (never re-asked,
        never taken from ``recorded``)."""
        return self._build(validated_task(task), {stage.stage_id: stage for stage in recorded.stages})

    def _build(self, task: TaskContract, recorded: Mapping[str, PlannedStage] | None) -> ExecutionPlan:
        if task.inputs:
            try:
                check_task_inputs(task, self._provenance)
            except ProvenanceRefused as exc:
                return self._finish(task, None, None, None, (), (exc.code,))
        try:
            template, graph = self._templates.decompose(task)
        except GraphError as exc:
            return self._finish(task, None, None, None, (), (exc.code,))
        stages = tuple(self._plan_stage(task, graph, stage_id, None if recorded is None else recorded.get(stage_id),
                                        template.template_hash)
                       for stage_id in (stage.stage_id for stage in graph.ordered()))
        codes = [f"{stage.stage_id}:{stage.infeasible_code}" for stage in stages if stage.infeasible_code]
        codes.extend(self._missing_outputs(task, graph))
        return self._finish(task, template.template_id, template.template_hash, graph.graph_hash, stages,
                            tuple(codes))

    def _finish(self, task: TaskContract, template_id: str | None, template_hash: str | None,
                graph_hash: str | None, stages: tuple[PlannedStage, ...], codes: tuple[str, ...]) -> ExecutionPlan:
        status = PlanStatus.FEASIBLE if stages and not codes else PlanStatus.INFEASIBLE
        advice_enabled = self._config.advice_enabled and self._advisor is not None
        document = plan_document(status, task.task_id, task.task_hash, template_id, template_hash, graph_hash,
                                 stages, codes, advice_enabled, task.human_gate, task.quality)
        digest = canonical_hash(document)
        return ExecutionPlan(f"plan-{digest[:24]}", digest, status, task.task_id, task.task_hash, template_id,
                             template_hash, graph_hash, stages, codes, advice_enabled, task.human_gate, task.quality)

    def _missing_outputs(self, task: TaskContract, graph: CapabilityGraph) -> list[str]:
        produced: set[str] = set()
        for stage_id in graph.terminal_stage_ids():
            try:
                produced.add(self._registry.capability(graph.stage(stage_id).capability_id).output_contract)
            except RegistryError:
                continue
        return [f"REQUIRED_OUTPUT_NOT_PRODUCED:{ref}" for ref in task.required_outputs if ref not in produced]

    def _plan_stage(self, task: TaskContract, graph: CapabilityGraph, stage_id: str,
                    recorded: PlannedStage | None, template_hash: str) -> PlannedStage:
        stage = graph.stage(stage_id)
        base: dict[str, Any] = dict(
            stage_id=stage.stage_id, capability_id=stage.capability_id, capability_family=None,
            capability_version=None, adapter_version=None, determinism=None, capability_digest=None, inputs=stage.inputs, parameters=stage.parameters, seed=stage.seed,
            candidates=(), ordering=(), selected_adapter_id=None, adapter_digest=None, decision_hash=None,
            resource_id=UNASSIGNED_RESOURCE, alternatives=(), advice=(), advice_status=None,
            advice_context_hash=None, router_selection_hash=None, router_status=None, infeasible_code=None)
        try:
            capability = self._registry.capability(stage.capability_id)
        except RegistryError as exc:
            return PlannedStage(**{**base, "infeasible_code": exc.code})
        base.update(capability_family=capability.family, capability_digest=descriptor_digest(capability),
                    capability_version=capability.version)
        if capability.determinism is Determinism.SEEDED and stage.seed is None:
            return PlannedStage(**{**base, "infeasible_code": "SEEDED_STAGE_REQUIRES_SEED"})
        if task.quality is QualityRequirement.VERIFIED and not _AUTOMATIC.intersection(capability.verification):
            return PlannedStage(**{**base, "infeasible_code": "QUALITY_REQUIREMENT_UNMET"})
        try:
            scope = authorize_stage(task, stage.capability_id, self._grants)
        except PermissionError as exc:
            if str(exc) != "CAPABILITY_NOT_AUTHORIZED":
                return PlannedStage(**{**base, "infeasible_code": str(exc) or "GRANT_INVALID"})
            try:
                scope = _diagnostic_scope(task, self._grants)
            except (PermissionError, LookupError):
                return PlannedStage(**{**base, "infeasible_code": "CAPABILITY_NOT_AUTHORIZED"})
        adapters = self._registry.adapters_for(stage.capability_id)
        if not adapters:
            return PlannedStage(**{**base, "infeasible_code": "NO_REGISTERED_ADAPTER"})
        records: list[CandidateRecord] = []
        feasible: dict[str, tuple[AdapterDescriptor, str, str]] = {}
        for adapter in adapters:
            record, allowed = self._evaluate(task, capability, adapter, scope, stage.stage_id)
            records.append(record)
            if allowed is not None:
                feasible[adapter.adapter_id] = allowed
        base["candidates"] = tuple(records)
        if not feasible:
            return PlannedStage(**{**base, "infeasible_code": "NO_FEASIBLE_CANDIDATE"})
        ordering, advice, advice_status, context_hash = self._order(task, capability, feasible, recorded,
                                                                    template_hash=template_hash)
        base.update(ordering=ordering, advice=advice, advice_status=advice_status, advice_context_hash=context_hash)
        if capability.family is CapabilityFamily.MODEL_RUNTIME:
            if self._inference is None:
                return PlannedStage(**{**base, "infeasible_code": "MODEL_INFERENCE_DELEGATE_MISSING"})
            selection = self._inference.select(InferenceRequest(
                project_id=task.project_id, stage_id=stage.stage_id, capability_id=capability.capability_id,
                risk_level=task.risk_level.value, classification=task.classification,
                network_policy=task.network_policy.value, grant_hash=scope.grant_hash))
            base.update(router_selection_hash=selection.selection_hash, router_status=selection.status)
            if selection.status != "ROUTED" or selection.selected_worker_id is None:
                return PlannedStage(**{**base, "infeasible_code": "WORKER_ROUTER_BLOCKED"})
        selected = ordering[0]
        descriptor, decision_hash, resource_id = feasible[selected]
        alternatives = ordering[1:] if capability.fallback is FallbackPolicy.EXPLICIT_SAME_POLICY else ()
        return PlannedStage(**{**base, "selected_adapter_id": selected, "adapter_version": descriptor.version,
                               "determinism": descriptor.determinism,
                               "adapter_digest": descriptor_digest(descriptor), "decision_hash": decision_hash,
                               "resource_id": resource_id, "alternatives": alternatives})

    def _evaluate(self, task: TaskContract, capability: CapabilityDescriptor, adapter: RegisteredAdapter,
                  scope: AuthorizedScope, stage_id: str
                  ) -> tuple[CandidateRecord, tuple[AdapterDescriptor, str, str] | None]:
        try:
            descriptor = adapter.descriptor
        except AdapterIntegrityError as exc:
            return CandidateRecord(adapter.adapter_id, None, str(exc), None, UNASSIGNED_RESOURCE, (), ()), None
        match = resource_match(self._inventory, descriptor)
        context = PolicyContext(scope=scope, project_id=task.project_id, stage_id=stage_id,
                                classification=task.classification, network_policy=task.network_policy,
                                resource_id=match.resource_id)
        decision = evaluate(capability, descriptor, context, adapter_available=adapter.available,
                            resource_fit=match.fits)
        failing = tuple(name for name, ok in decision.checks if not ok)
        record = CandidateRecord(adapter.adapter_id, descriptor_digest(descriptor), decision.code.value,
                                 decision.decision_hash, match.resource_id, match.reasons, failing)
        if decision.code is not PolicyCode.ALLOWED:
            return record, None
        return record, (descriptor, decision.decision_hash, match.resource_id)

    def _order(self, task: TaskContract, capability: CapabilityDescriptor,
               feasible: Mapping[str, tuple[AdapterDescriptor, str, str]], recorded: PlannedStage | None = None,
               *, template_hash: str
               ) -> tuple[tuple[str, ...], tuple[AdviceRecord, ...], str | None, str | None]:
        preferred = [aid for aid in dict.fromkeys(self._config.preferences.get(capability.capability_id, ()))
                     if aid in feasible]
        rest = sorted(aid for aid in feasible if aid not in preferred)
        if not (self._config.advice_enabled and self._advisor is not None):
            return (*preferred, *rest), (), None, None
        # Task type, capability, classification and the feasible set are explicit binding inputs; the context
        # features are the advisor's closed bucketing vocabulary, which the task contract does not carry (V1).
        features: dict[str, str] = {}
        candidates = tuple(sorted((aid, feasible[aid][0].version) for aid in feasible))
        binding = plan_binding_hash(task, template_hash, capability.capability_id, candidates)
        try:
            context_hash = self._advisor.context_hash(task_type=task.task_type,
                                                      capability_id=capability.capability_id,
                                                      context_features=features, candidates=candidates,
                                                      classification=task.classification.value,
                                                      plan_binding_hash=binding)
        except Exception:  # noqa: BLE001 - invalid features mean "no advice", never a failed plan
            return (*preferred, *rest), (), "ADVICE_CONTEXT_INVALID", None
        if not isinstance(context_hash, str) or not _SHA256.fullmatch(context_hash):
            return (*preferred, *rest), (), "ADVICE_CONTEXT_INVALID", None
        advise_failed = False
        if recorded is None:  # live planning asks once (the advisor computes and records); reproduction never
            try:
                self._advisor.advise(task_type=task.task_type, capability_id=capability.capability_id,
                                     candidates=[feasible[aid][0] for aid in (*preferred, *rest)],
                                     context_features=features, classification=task.classification.value,
                                     plan_binding_hash=binding)
            except Exception:  # noqa: BLE001 - advice is optional; a failing advisor never changes feasibility
                advise_failed = True
        issued = _issued(self._advisor, binding, candidates)
        if issued is None:
            if recorded is None:
                status = "ADVICE_ERROR" if advise_failed else "ADVICE_NOT_ISSUED"
            else:  # nothing was issued: the plan may carry no advice (its no-advice status is diagnostic only)
                status = (str(recorded.advice_status) if recorded.advice_status in _NOT_ISSUED and not recorded.advice
                          else "ADVICE_NOT_ISSUED")
            return (*preferred, *rest), (), status, context_hash
        return _issued_order(preferred, rest, issued, context_hash)


def plan_binding_hash(task: TaskContract, template_hash: str, capability_id: str,
                      candidates: tuple[tuple[str, str], ...]) -> str:
    """Advice-independent binding of one advice request to this task, template, capability and feasible set."""
    return canonical_hash({"kind": "pados.capability.advice-binding.v1", "task_hash": task.task_hash,
                           "template_hash": template_hash, "capability_id": capability_id,
                           "candidates": [list(item) for item in candidates]})


def _issued(advisor: ScoreAdvisor, binding: str, candidates: tuple[tuple[str, str], ...]
            ) -> tuple[Advice, ...] | None:
    """The advice the advisor reports as issued for ``binding``; anything malformed counts as not issued."""
    try:
        raw = advisor.issued(binding, candidates=candidates)
        if raw is None:
            return None
        items = tuple(raw)
    except Exception:  # noqa: BLE001 - an advisor that cannot answer issued nothing
        return None
    if len(items) > MAX_ISSUED_ADVICE or not all(
            isinstance(item, Advice) and isinstance(item.adapter_id, str) and isinstance(item.sample_size, int)
            and isinstance(item.profile_hash, str) and _SHA256.fullmatch(item.profile_hash)
            and isinstance(item.explanation, str) for item in items):
        return None
    return items


def _issued_order(preferred: Sequence[str], rest: Sequence[str], issued: Sequence[Advice], context_hash: str
                  ) -> tuple[tuple[str, ...], tuple[AdviceRecord, ...], str | None, str | None]:
    """Apply the ISSUED advice: clamped, feasible-only, context-bound, never across an explicit preference."""
    try:
        adjustments = apply_advice((*preferred, *rest), issued, context_hash=context_hash)
    except AdviceRejected as exc:
        return (*preferred, *rest), (), str(exc), context_hash
    by_id = {item.adapter_id: item for item in issued}
    records = tuple(AdviceRecord(aid, adjustments[aid], by_id[aid].sample_size, by_id[aid].profile_hash,
                                 canonical_hash({"explanation": by_id[aid].explanation}), aid in preferred)
                    for aid in sorted(adjustments))
    return _advised_order(preferred, rest, adjustments), records, "APPLIED", context_hash


def _advised_order(preferred: Sequence[str], rest: Sequence[str], adjustments: Mapping[str, float]) -> tuple[str, ...]:
    """Explicit preferences first (advice can never cross them), then bounded advice, then adapter_id."""
    scores = {aid: -_PREFERRED_TIER_GAP * index for index, aid in enumerate(preferred)}
    top = -_PREFERRED_TIER_GAP * len(preferred)
    for aid in rest:
        scores[aid] = top - _PREFERRED_TIER_GAP + adjustments.get(aid, 0.0)
    return tuple(preferred) + tuple(sorted(rest, key=lambda aid: (-scores[aid], aid)))


def resource_match(inventory: ResourceInventory | None, descriptor: AdapterDescriptor) -> ResourceMatch:
    """The first fitting resource in resource_id order; no inventory -> the unassigned local resource."""
    if inventory is None:
        return ResourceMatch(UNASSIGNED_RESOURCE, True, ())
    try:
        matches = sorted(inventory.match(descriptor.resources), key=lambda item: str(item.resource_id))
    except Exception:  # noqa: BLE001 - an inventory that cannot answer means "does not fit" (fail closed)
        return ResourceMatch(UNASSIGNED_RESOURCE, False, ("RESOURCE_INVENTORY_ERROR",))
    for match in matches:
        if match.fits is True and isinstance(match.resource_id, str) and _RESOURCE_ID.fullmatch(match.resource_id):
            return ResourceMatch(match.resource_id, True, tuple(str(r)[:64] for r in match.reasons)[:16])
    reasons = tuple(sorted({str(reason)[:64] for match in matches for reason in match.reasons}))[:16]
    return ResourceMatch(UNASSIGNED_RESOURCE, False, reasons or ("NO_RESOURCE_FITS",))
