# SPDX-License-Identifier: AGPL-3.0-only
"""LocalInferenceProvider: node-backed local inference through the existing
PADOS Worker Router.

No second router: eligible (node, model) pairs are presented to
``pados.worker.router.select_worker`` as candidates of the router's existing
local-model worker class (``WorkerType.OLLAMA``), and the router makes the
selection. Classification, authorization, capability, model availability and
resource state are enforced as candidate eligibility before the router sees
them, using the Enterprise Pack contracts (classification, ComputeNode,
ProviderCapabilityProfile, SecretRef, observability).

Model authorization is explicit: a registration authorizes only the model ids it NAMES in ``allowed_models``;
an empty list authorizes no model (never a wildcard).

Measured execution gate (optional ``MeasuredExecutionGate``): the provider then routes only to models that carry
a REGISTERED measured profile in the existing ``ModelCatalog`` (matched by manifest digest), whose immutable model
reference the CURRENT registration names explicitly, and whose live runtime binding (runtime, device, freshness,
resources) and measured evidence satisfy the capability requirement. ``execute`` re-checks exactly those
conditions from current authority immediately before the job is submitted (any change refuses, nothing runs) and
again after the job finished (any change withholds the output, ``REVOKED_DURING_EXECUTION``). A job already running
on the node is not cancelled by a revocation; only its output is withheld. The gate reads authority, never
writes it, and never selects: selection stays with the Worker Router.

Measured execution configuration: a capability is measured under MODEL x RUNTIME x DEVICE x EXECUTION CONFIG x
EVIDENCE. Admission binds the ONE measured execution condition the requirement's capabilities were measured under
(``required_execution``); ``execute`` derives the effective configuration from that binding (only the output budget
and the output constraint come from the caller), refuses anything not ``COMPATIBLE`` with
``MEASURED_EXECUTION_CONFIG_MISMATCH`` before the node is contacted, sends the configuration to the node (whose
backend applies it or refuses it) and requires the signed node record to echo exactly that configuration. A caller
cannot pass another configuration: there is no parameter for it.

Failure evidence: a node job that ends in any state other than SUCCEEDED raises ``NodeExecutionError`` carrying
``failure`` (request and job ids, terminal state, the complete normalized backend code and its bounded detail, the
record MAC verification state and the signed node record itself). No node execution receipt is ever produced for a
failed job.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pados.compute_node.backends import safe_detail
from pados.compute_node.identity import record_mac, verify
from pados.compute_node.manifest import (
    ModelAvailability,
    ModelManifest,
    NodeCapability,
    NodeDescriptor,
    NodeState,
)
from pados.compute_node.receipts import node_execution_receipt
from pados.compute_node.registry import (
    HttpFactory,
    NodeClientError,
    NodeProber,
    NodeRegistration,
    NodeRegistry,
)
from pados.enterprise import observability
from pados.enterprise.classification import DataClassification, lowest
from pados.enterprise.compute import Locality, ProviderCapabilityProfile
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary, SecretResolver
from pados.resources.capability_profile import (
    MEASURED_EXECUTION_CONFIG_MISMATCH,
    CapabilityRequirement,
    ExecutionCompatibility,
    ExecutionConfig,
    LiveRuntimeBinding,
    MeasuredExecutionCondition,
    ModelCapabilityProfile,
    OutputConstraint,
    assess,
    check_execution,
    required_execution,
)
from pados.resources.models import ModelCatalog
from pados.worker.router import select_worker
from pados.worker.schemas import CapabilityId, RiskLevel, WorkerAvailability, WorkerSpec, WorkerType

PROVIDER_ID = "PADOS_LOCAL_INFERENCE"
# Router capability used for the fit/availability/risk decision. The node
# capability itself is enforced by registration + manifest gates below.
ROUTER_CAPABILITY = {
    NodeCapability.LOCAL_INFERENCE: CapabilityId.SUMMARIZE,
    NodeCapability.EMBEDDING: CapabilityId.READ_REPOSITORY,
    NodeCapability.CLASSIFICATION: CapabilityId.READ_REPOSITORY,
}
LOCAL_MODEL_WORKER = WorkerType.OLLAMA


class RoutingDenied(PermissionError):
    def __init__(self, code: str, considered: list[dict[str, Any]]) -> None:
        super().__init__(code)
        self.code = code
        self.considered = considered


class NodeExecutionError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool = False, failure: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.failure = failure  # diagnostic evidence of a failed node job (never a receipt, never authority)


@dataclass(frozen=True)
class RouteIntent:
    capability: NodeCapability
    classification: DataClassification
    project_id: str
    model_id: str | None = None
    min_context_tokens: int = 1
    profile_id: str | None = None  # pin to one measured profile (the one the Worker Router selected)


@dataclass(frozen=True)
class MeasuredExecutionGate:
    """Current authority the gate re-reads on every route and execution (each callable returns a fresh snapshot).

    ``catalog``: the existing ModelCatalog (REGISTERED measured profiles). ``bindings``: the live runtime binding
    of a profile (``pados.local_intelligence.live``). ``requirements``: the operator's measured requirement per
    node capability (unmapped -> refused). ``registrations``: the CURRENT compute-node registrations (default: the
    provider's registry)."""

    catalog: Callable[[], ModelCatalog]
    bindings: Callable[[ModelCapabilityProfile], LiveRuntimeBinding]
    requirements: Mapping[NodeCapability, CapabilityRequirement]
    registrations: Callable[[], Iterable[NodeRegistration]] | None = None


@dataclass(frozen=True)
class MeasuredRoute:
    """The measured conditions a route was admitted under (re-checked before and after execution)."""

    profile_id: str
    manifest_digest: str
    model_ref: str
    requirement_hash: str
    binding_hash: str
    assessment_state: str
    reasons: tuple[str, ...] = ()
    execution: MeasuredExecutionCondition | None = None  # the measured condition every execution must reproduce

    @property
    def execution_hash(self) -> str | None:
        return None if self.execution is None else self.execution.config.config_hash


@dataclass(frozen=True)
class NodeRoute:
    registration: NodeRegistration
    descriptor: NodeDescriptor
    model: ModelManifest
    profile: ProviderCapabilityProfile
    intent: RouteIntent
    worker_id: str
    considered: list[dict[str, Any]] = field(default_factory=list)
    measured: MeasuredRoute | None = None


@dataclass(frozen=True)
class NodeExecution:
    route: NodeRoute
    record: dict[str, Any]
    artifact_bytes: bytes
    receipt: dict[str, Any]


class LocalInferenceProvider:
    """Routes to and executes on registered, authorized, probed local compute nodes."""

    provider_id = PROVIDER_ID

    def __init__(self, registry: NodeRegistry, *, resolver: SecretResolver | None = None,
                 http: HttpFactory | None = None, poll_interval: float = 0.05,
                 measured: MeasuredExecutionGate | None = None) -> None:
        self.registry = registry
        self.resolver = resolver or EnvironmentSecretResolver()
        self.prober = NodeProber(registry, resolver=self.resolver, http=http)
        self.poll_interval = poll_interval
        self.measured = measured

    # -- measured gate -------------------------------------------------------
    def _requirement(self, intent: RouteIntent) -> CapabilityRequirement:
        assert self.measured is not None
        requirement = self.measured.requirements.get(intent.capability)
        if requirement is None:
            raise RoutingDenied("MEASURED_REQUIREMENT_UNMAPPED", [])
        if intent.min_context_tokens > requirement.min_context_tokens and intent.min_context_tokens > 1:
            requirement = requirement.model_copy(update={"min_context_tokens": intent.min_context_tokens})
        return requirement

    def measured_check(self, registration: NodeRegistration, model: ModelManifest, intent: RouteIntent,
                       requirement: CapabilityRequirement) -> tuple[str | None, MeasuredRoute | None]:
        """(refusal code, None) or (None, admitted conditions), from CURRENT authority only. Never raises."""
        gate = self.measured
        if gate is None:
            return "MEASURED_GATE_NOT_CONFIGURED", None
        try:
            digest = _normalized_digest(model.digest)
            profile = next((p for p in gate.catalog().capability_profiles()
                            if digest is not None and p.artifact.manifest_digest == digest), None)
            if profile is None:
                return "MODEL_PROFILE_NOT_REGISTERED", None
            if intent.profile_id is not None and profile.profile_id != intent.profile_id:
                return "MODEL_PROFILE_NOT_SELECTED", None
            current = [r for r in (gate.registrations or (lambda: self.registry.nodes))()
                       if r.node_id == registration.node_id]
            if len(current) != 1 or current[0].endpoint != registration.endpoint \
                    or current[0].auth_ref != registration.auth_ref:
                return "NODE_REGISTRATION_CHANGED", None
            now = current[0]
            if _registration_gate(now, intent) is not None or model.model_id not in now.allowed_models \
                    or profile.artifact.model_ref not in now.allowed_models:
                return "MODEL_NOT_AUTHORIZED", None
            binding = gate.bindings(profile)
            if binding.profile_id != profile.profile_id or binding.manifest_digest != digest:
                return "LIVE_BINDING_MISMATCH", None
            assessment = assess(profile, requirement, runtime_state=binding.runtime_state,
                                device_state=binding.device_state, headroom=binding.headroom)
            reasons = tuple(dict.fromkeys((*assessment.reasons, *binding.reasons)))
            if not assessment.eligible(requirement):
                return "MEASURED_INELIGIBLE:" + ",".join(reasons)[:200], None
            condition, unbound = required_execution(profile, requirement)
            if condition is None:
                return unbound or "MEASURED_EXECUTION_CONFIG_MISSING", None
            return None, MeasuredRoute(profile.profile_id, digest, profile.artifact.model_ref,
                                       requirement.requirement_hash, binding.binding_hash, assessment.state.value,
                                       reasons, condition)
        except Exception as exc:  # noqa: BLE001 - an authority that cannot answer admits nothing (fail closed)
            return f"MEASURED_AUTHORITY_UNAVAILABLE:{type(exc).__name__}", None

    def _revalidate(self, route: NodeRoute, phase: str) -> None:
        """Re-derive the measured admission from current authority; refuse on any difference."""
        if self.measured is None:
            return
        if route.measured is None:
            raise NodeExecutionError(f"{phase}:MEASURED_ROUTE_REQUIRED")
        try:
            requirement = self._requirement(route.intent)
        except RoutingDenied as exc:
            raise NodeExecutionError(f"{phase}:{exc.code}") from None
        evidence = self.prober.probe(route.registration)
        if evidence["state"] != NodeState.ONLINE.value or evidence["descriptor"] is None:
            raise NodeExecutionError(f"{phase}:NODE_{evidence['state']}")
        descriptor = NodeDescriptor.model_validate(evidence["descriptor"])
        model = next((m for m in descriptor.models if m.model_id == route.model.model_id), None)
        if descriptor.manifest_hash != route.descriptor.manifest_hash or model is None \
                or model.availability is not ModelAvailability.AVAILABLE:
            raise NodeExecutionError(f"{phase}:NODE_MODEL_CHANGED")
        code, admitted = self.measured_check(route.registration, model, route.intent, requirement)
        if code is not None or admitted is None:
            raise NodeExecutionError(f"{phase}:{code}")
        if (admitted.profile_id, admitted.manifest_digest, admitted.requirement_hash, admitted.execution) != (
                route.measured.profile_id, route.measured.manifest_digest, route.measured.requirement_hash,
                route.measured.execution):
            raise NodeExecutionError(f"{phase}:MEASURED_ADMISSION_CHANGED")

    @staticmethod
    def effective_execution(route: NodeRoute, inputs: list[dict[str, Any]],
                            max_output_tokens: int | None) -> tuple[ExecutionConfig | None, tuple[str, ...]]:
        """The configuration this execution will run under: the route's measured condition with the caller's
        output budget and output constraint. (config, ()) when COMPATIBLE, else (None, refusal reasons)."""
        condition = route.measured.execution if route.measured is not None else None
        if condition is None:
            return None, ("MEASURED_EXECUTION_CONFIG_MISSING",)
        schemas = [isinstance(item.get("response_schema"), dict) and bool(item["response_schema"]) for item in inputs]
        if any(schemas) and not all(schemas):
            return None, (f"{MEASURED_EXECUTION_CONFIG_MISMATCH}:output_constraint",)
        default_budget = min(condition.config.max_output_tokens, route.descriptor.limits.max_output_tokens)
        effective = condition.config.model_copy(update={
            "max_output_tokens": max_output_tokens if max_output_tokens is not None else default_budget,
            "output_constraint": OutputConstraint.JSON_SCHEMA if schemas and all(schemas)
            else OutputConstraint.UNCONSTRAINED})
        compatibility, reasons = check_execution(condition, effective)
        return (effective, ()) if compatibility is ExecutionCompatibility.COMPATIBLE else (None, reasons)

    # -- routing -----------------------------------------------------------
    def route(self, intent: RouteIntent) -> NodeRoute:
        considered: list[dict[str, Any]] = []
        eligible: list[tuple[NodeRegistration, NodeDescriptor, ModelManifest, ProviderCapabilityProfile]] = []
        requirement = self._requirement(intent) if self.measured is not None else None
        admitted: dict[tuple[str, str], MeasuredRoute] = {}
        for registration in self.registry.nodes:
            entry: dict[str, Any] = {"node_id": registration.node_id}
            considered.append(entry)
            gate = _registration_gate(registration, intent)
            if gate:
                entry["reason"] = gate
                continue
            evidence = self.prober.probe(registration)
            entry["state"] = evidence["state"]
            if evidence["state"] != NodeState.ONLINE.value or evidence["descriptor"] is None:
                entry["reason"] = f"NODE_{evidence['state']}:" + ",".join(evidence["reasons"][:3])
                continue
            descriptor = NodeDescriptor.model_validate(evidence["descriptor"])
            ceiling = lowest(registration.max_classification, descriptor.privacy_class)
            if not descriptor.as_compute_node().eligible_for(intent.classification, intent.capability.value) \
                    or not ceiling.permits(intent.classification):
                entry["reason"] = "NODE_CAPABILITY_OR_CLASSIFICATION_INELIGIBLE"
                continue
            models = [m for m in descriptor.models if _model_ok(m, registration, intent)]
            if not models:
                entry["reason"] = "NO_AVAILABLE_MODEL"
                continue
            if requirement is not None:
                refused: list[str] = []
                for model in list(models):
                    code, measured = self.measured_check(registration, model, intent, requirement)
                    if measured is None:
                        refused.append(f"{model.model_id}:{code}")
                        models.remove(model)
                    else:
                        admitted[(registration.node_id, model.model_id)] = measured
                if refused:
                    entry["measured_refusals"] = refused[:8]
                if not models:
                    entry["reason"] = "NO_MEASURED_ELIGIBLE_MODEL"
                    continue
            for model in models:
                profile = ProviderCapabilityProfile(
                    provider_id=_worker_ref(descriptor.node_id, model.model_id), locality=Locality.LOCAL,
                    accelerators=descriptor.accelerators, privacy_class=lowest(ceiling, model.privacy_class),
                    context_limit_tokens=model.context_limit_tokens, model_family=model.family,
                    supported_tasks=tuple(t.value for t in model.tasks))
                if profile.permits(intent.classification):
                    eligible.append((registration, descriptor, model, profile))
            if not any(item[0] is registration for item in eligible):
                entry["reason"] = "MODEL_PRIVACY_INELIGIBLE"
        eligible.sort(key=lambda item: (item[1].current_load.running + item[1].current_load.queued,
                                        item[1].node_id, item[2].model_id))
        workers = {_worker_ref(d.node_id, m.model_id): WorkerSpec(
            worker_id=_worker_ref(d.node_id, m.model_id), worker_type=LOCAL_MODEL_WORKER,
            display_name=f"{d.node_name} / {m.model_id}", version=d.runtime_version,
            availability=WorkerAvailability.AVAILABLE, capabilities=[ROUTER_CAPABILITY[intent.capability]],
            risk_ceiling=RiskLevel.MEDIUM, supports_structured_output=True, supports_file_changes=False,
            supports_tool_calls=False, supports_streaming=False, max_context_tokens=m.context_limit_tokens,
            default_timeout_seconds=int(d.limits.default_timeout_seconds)) for _, d, m, _ in eligible}
        decision = select_worker(workers, capability_id=ROUTER_CAPABILITY[intent.capability], risk_level=RiskLevel.LOW,
                                 allowed_worker_types=frozenset({LOCAL_MODEL_WORKER}))
        if decision.selected_worker_id is None:
            code = "NO_ELIGIBLE_LOCAL_NODE" if not eligible else str(decision.error_code)
            observability.emit("authorization.decision", decision="DENY", reason=code,
                               classification=intent.classification.value, project_id=intent.project_id)
            raise RoutingDenied(code, considered)
        registration, descriptor, model, profile = next(
            item for item in eligible if _worker_ref(item[1].node_id, item[2].model_id) == decision.selected_worker_id)
        observability.emit("node.selected", node_id=descriptor.node_id, model_id=model.model_id,
                           provider_id=PROVIDER_ID, classification=intent.classification.value,
                           execution_class="LOCAL", project_id=intent.project_id)
        return NodeRoute(registration, descriptor, model, profile, intent, decision.selected_worker_id, considered,
                         admitted.get((registration.node_id, model.model_id)))

    # -- execution ---------------------------------------------------------
    def execute(self, route: NodeRoute, *, run_id: str, work_package_id: str, inputs: list[dict[str, Any]],
                output_path: str, allowed_paths: tuple[str, ...], timeout_seconds: float,
                max_output_tokens: int | None = None) -> NodeExecution:
        if output_path not in allowed_paths:
            raise NodeExecutionError("ARTIFACT_PATH_UNAUTHORIZED")
        self._revalidate(route, "EXECUTION_GATE_REFUSED")  # before anything is submitted to the node
        execution: ExecutionConfig | None = None
        if self.measured is not None:  # the bound measured configuration, or nothing runs
            execution, refused = self.effective_execution(route, inputs, max_output_tokens)
            if execution is None:
                raise NodeExecutionError("EXECUTION_GATE_REFUSED:" + ",".join(refused)[:240])
            max_output_tokens = execution.max_output_tokens
        registration = route.registration
        request = {
            "request_id": f"{run_id}:{uuid.uuid4()}", "work_package_id": work_package_id, "run_id": run_id,
            "capability": route.intent.capability.value, "model_id": route.model.model_id,
            "classification": route.intent.classification.value, "inputs": inputs, "output_path": output_path,
            "max_output_tokens": max_output_tokens, "timeout_seconds": timeout_seconds,
            "execution": None if execution is None else execution.model_dump(mode="json"),
        }
        input_sha = hashlib.sha256(json.dumps(inputs, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        boundary = SecretBoundary(self.resolver)
        with boundary.resolved({registration.auth_ref}, authorized_refs={registration.auth_ref}) as values:
            credential = values[registration.auth_ref].reveal()
            client = self.prober.client(registration, credential)
            try:
                record = self._run(client, request, timeout_seconds, credential)
                self._verify_record(record, request, input_sha, route, credential)
                artifact = record["artifacts"][0]
                try:
                    payload, header = client.artifact(artifact["artifact_id"])
                except NodeClientError as exc:
                    raise NodeExecutionError("ARTIFACT_MISSING" if exc.status == 404 else
                                             "ARTIFACT_TAMPERED" if exc.status == 409 else exc.code) from None
            finally:
                client.close()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != artifact["sha256"] or len(payload) != artifact["size_bytes"] or (header and header != digest):
            raise NodeExecutionError("ARTIFACT_HASH_MISMATCH")
        try:  # the job already ran; a change since admission withholds its output (no cancellation is implied)
            self._revalidate(route, "REVOKED_DURING_EXECUTION")
        except NodeExecutionError:
            raise NodeExecutionError("REVOKED_DURING_EXECUTION") from None
        verification = {"record_mac": "VERIFIED", "identity": "VERIFIED", "artifact_sha256": "VERIFIED",
                        "manifest_hash": "VERIFIED", "request_binding": "VERIFIED"}
        observability.emit("provider.completed", node_id=route.descriptor.node_id, model_id=route.model.model_id,
                           provider_id=PROVIDER_ID, status=record["state"], latency_ms=record.get("inference_ms"),
                           queue_delay_ms=record.get("queue_delay_ms"), execution_class="LOCAL")
        receipt = node_execution_receipt(record=record, provider_id=PROVIDER_ID, credential_ref=registration.auth_ref,
                                         execution_receipt_id=None, verification=verification)
        return NodeExecution(route, record, payload, receipt)

    def _run(self, client: Any, request: dict[str, Any], timeout: float, credential: str) -> dict[str, Any]:
        try:
            record = client.execute(request)
        except NodeClientError as exc:
            raise NodeExecutionError(exc.code, retryable=exc.code in {"QUEUE_FULL", "NODE_TIMEOUT"},
                                     failure=_failure(request, None, "NOT_ADMITTED", exc.code)) from None
        deadline = time.monotonic() + timeout + 5
        while record.get("state") in {"QUEUED", "RUNNING"}:
            if time.monotonic() > deadline:
                try:
                    client.cancel(str(record["job_id"]))
                except NodeClientError:
                    pass
                raise NodeExecutionError("JOB_TIMED_OUT", retryable=True,
                                         failure=_failure(request, record, "CLIENT_DEADLINE", "JOB_TIMED_OUT"))
            time.sleep(self.poll_interval)
            try:
                record = client.job(str(record["job_id"]))
            except NodeClientError as exc:
                raise NodeExecutionError(exc.code, retryable=True,
                                         failure=_failure(request, record, "POLL_FAILED", exc.code)) from None
        mac_ok = verify(record_mac(credential, record), record.get("record_mac"))
        if not mac_ok:
            raise NodeExecutionError("NODE_RECEIPT_INVALID",
                                     failure=_failure(request, record, "RECORD_MAC_INVALID", "NODE_RECEIPT_INVALID"))
        state = record.get("state")
        if state != "SUCCEEDED":
            code = f"JOB_{_bounded_code(state)}:{_bounded_code(record.get('error_code'))}"
            raise NodeExecutionError(code, retryable=state == "TIMED_OUT",
                                     failure=_failure(request, record, str(state), code, mac_verified=True))
        if record.get("execution") != request.get("execution") or (
                request.get("execution") is not None
                and (record.get("runtime") or {}).get("execution_requested") != request["execution"]):
            # the echo proves the configuration reached the runtime request; observable behaviour (reasoning
            # despite the switch, truncation, empty output) is checked by the backend itself
            raise NodeExecutionError("MEASURED_EXECUTION_CONFIG_NOT_APPLIED", failure=_failure(
                request, record, "SUCCEEDED_UNBOUND", "MEASURED_EXECUTION_CONFIG_NOT_APPLIED", mac_verified=True))
        return dict(record)

    @staticmethod
    def _verify_record(record: dict[str, Any], request: dict[str, Any], input_sha: str, route: NodeRoute,
                       credential: str) -> None:
        del credential
        binding = {k: request[k] for k in ("request_id", "work_package_id", "run_id", "capability", "model_id",
                                           "classification", "output_path")}
        if record.get("node_id") != route.registration.node_id or any(record.get(k) != v for k, v in binding.items()) \
                or record.get("input_sha256") != input_sha:
            raise NodeExecutionError("NODE_RECEIPT_INVALID")
        if record.get("manifest_hash") != route.descriptor.manifest_hash:
            raise NodeExecutionError("NODE_MANIFEST_STALE")
        artifacts = record.get("artifacts") or []
        if not artifacts:
            raise NodeExecutionError("ARTIFACT_MISSING")
        if len(artifacts) != 1:
            raise NodeExecutionError("ARTIFACT_UNEXPECTED")
        if artifacts[0].get("path") != request["output_path"]:
            raise NodeExecutionError("ARTIFACT_PATH_UNAUTHORIZED")


FAILURE_SCHEMA = "pados.compute-node.failure-evidence.v1"
MAX_EVIDENCE_RECORD_CHARS = 32_768
_CODE_UNSAFE = re.compile(r"[^A-Za-z0-9_:.\-]")


def _bounded_code(value: object) -> str | None:
    """A node-supplied code as evidence: code characters only, bounded (never free text)."""
    return None if value is None else _CODE_UNSAFE.sub("_", str(value))[:160]


def _failure(request: Mapping[str, Any], record: Mapping[str, Any] | None, terminal: str, code: str, *,
             mac_verified: bool = False) -> dict[str, Any]:
    """Diagnostic evidence of a failed node job: identities, the complete normalized failure code, the backend
    detail re-filtered here (counters and short labels only) and the node record (ids, hashes and codes; never the
    prompt, content or credential). Only a record whose MAC verified is kept whole, as ``signed_record``; any other
    record contributes nothing but its bounded codes."""
    raw_detail = None if record is None else record.get("error_detail")
    signed: dict[str, Any] | None = dict(record) if record is not None and mac_verified else None
    encoded = None if signed is None else json.dumps(signed, sort_keys=True, separators=(",", ":"))
    oversized = encoded is not None and len(encoded) > MAX_EVIDENCE_RECORD_CHARS
    return {
        "schema": FAILURE_SCHEMA, "request_id": request.get("request_id"),
        "job_id": None if record is None else _bounded_code(record.get("job_id")), "terminal_state": terminal,
        "job_state": None if record is None else _bounded_code(record.get("state")),
        "failure_code": _bounded_code(code),
        "backend_error_code": None if record is None else _bounded_code(record.get("error_code")),
        "backend_error_detail": safe_detail(raw_detail) if isinstance(raw_detail, dict) else None,
        "execution": request.get("execution"),
        "record_mac_verified": mac_verified, "node_receipt": "NOT_ISSUED_FOR_FAILED_EXECUTION",
        "signed_record": None if oversized else signed,
        "signed_record_sha256": None if encoded is None else hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "semantic_authority": "NONE",
    }


def _registration_gate(registration: NodeRegistration, intent: RouteIntent) -> str | None:
    if not registration.authorized:
        return "NODE_NOT_AUTHORIZED"
    if not registration.admits_project(intent.project_id):
        return "NODE_PROJECT_SCOPE_DENIED"
    if intent.capability not in registration.allowed_capabilities:
        return "NODE_CAPABILITY_NOT_AUTHORIZED"
    if not registration.max_classification.permits(intent.classification):
        return "NODE_CLASSIFICATION_CEILING"
    return None


def _worker_ref(node_id: str, model_id: str) -> str:
    """``lcn:<node>:<model id>``; a model id too long for a provider id (e.g. pinned to a full manifest digest) is
    referenced by a stable hash of the canonical id instead (the route keeps the full manifest)."""
    ref = f"lcn:{node_id}:{model_id}"
    return ref if len(ref) <= 80 else f"lcn:{node_id}:m-{hashlib.sha256(model_id.encode('utf-8')).hexdigest()[:32]}"


def _normalized_digest(value: str | None) -> str | None:
    if value is None:
        return None
    bare = value.lower().removeprefix("sha256:")
    return f"sha256:{bare}" if len(bare) == 64 and all(c in "0123456789abcdef" for c in bare) else None


def _model_ok(model: ModelManifest, registration: NodeRegistration, intent: RouteIntent) -> bool:
    return (model.availability is ModelAvailability.AVAILABLE and intent.capability in model.tasks
            and model.model_id in registration.allowed_models  # explicit only: an empty list authorizes no model
            and (intent.model_id is None or model.model_id == intent.model_id)
            and model.context_limit_tokens >= intent.min_context_tokens)


class NodeModelBinding:
    """One routed (node, model) pair exposed through the Enterprise Pack
    ``LocalInferenceProvider`` protocol (``profile``/``health``/``generate``)."""

    def __init__(self, provider: LocalInferenceProvider, route: NodeRoute) -> None:
        self._provider = provider
        self._route = route

    @property
    def profile(self) -> ProviderCapabilityProfile:
        return self._route.profile

    def health(self) -> Any:
        evidence = self._provider.prober.probe(self._route.registration)
        return NodeState(evidence["state"]).health

    def generate(self, prompt: str, *, max_tokens: int) -> str:
        execution = self._provider.execute(
            self._route, run_id=f"generate-{uuid.uuid4()}", work_package_id="adhoc-generation",
            inputs=[{"messages": [{"role": "user", "content": prompt}]}], output_path="generation.txt",
            allowed_paths=("generation.txt",), timeout_seconds=self._route.descriptor.limits.default_timeout_seconds,
            max_output_tokens=max_tokens)
        return execution.artifact_bytes.decode("utf-8")
