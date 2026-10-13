# SPDX-License-Identifier: AGPL-3.0-only
"""Capability Fabric adapter: run a MODEL_RUNTIME stage on the measured local profile the Worker Router selected.

This adapter never selects. The fabric executor re-runs the Worker Router selection through the
``WorkerRouterBridge`` immediately before the stage (and after it, CF-03); this adapter resolves that SAME
selection (``bridge.issued``) to the measured local candidate behind it and executes on exactly that profile
through the existing ``LocalInferenceProvider`` with its ``MeasuredExecutionGate`` (``RouteIntent.profile_id`` pins
the route). Any refusal on the way is a closed adapter error; nothing falls back to another model or provider.
The node execution receipt is kept as evidence; model output is never authority (receipt -> validation -> CAS).

Input: each stage input artifact is a JSON prompt package ``{"messages": [{"role", "content"}, ...]}`` with an
optional ``"response_schema"`` (a JSON schema object): with it, output is requested schema-constrained, which is
then part of the effective execution configuration the measured gate compares with the measured one.

A failed node execution leaves its diagnostic failure evidence (``NodeExecutionError.failure``) in
``executions``; it never yields a receipt or an output.
"""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from typing import Any

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.inference import MeasuredLocalCandidates, WorkerRouterBridge, selection_routed
from pados.capability.model import AdapterDescriptor
from pados.compute_node.manifest import NodeCapability
from pados.compute_node.provider import LocalInferenceProvider, NodeExecutionError, RouteIntent, RoutingDenied

OUTPUT_PATH = "model-output.txt"
MAX_SCHEMA_CHARS = 16_384
_HASH = re.compile(r"^[0-9a-f]{64}$")


class LocalInferenceStageAdapter:
    def __init__(self, descriptor: AdapterDescriptor, *, bridge: WorkerRouterBridge,
                 measured: MeasuredLocalCandidates, provider: LocalInferenceProvider,
                 output_kind: str, capability: NodeCapability = NodeCapability.LOCAL_INFERENCE,
                 timeout_seconds: float = 120, max_output_tokens: int = 512,
                 run_ids: Callable[[], str] = lambda: f"stage-{uuid.uuid4()}") -> None:
        if not descriptor.delegates_to_worker_router:
            raise ValueError("LOCAL_INFERENCE_ADAPTER_MUST_DELEGATE_TO_WORKER_ROUTER")
        self._descriptor = descriptor
        self._bridge = bridge
        self._measured = measured
        self._provider = provider
        self._output_kind = output_kind
        self._capability = capability
        self._timeout = timeout_seconds
        self._max_tokens = max_output_tokens
        self._run_ids = run_ids
        self.executions: list[dict[str, Any]] = []  # node receipts and routes of this adapter's runs (evidence)

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return self._provider.measured is not None

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        selection_hash = request.router_selection_hash
        if not isinstance(selection_hash, str) or not _HASH.fullmatch(selection_hash):
            raise CapabilityAdapterError("WORKER_ROUTER_SELECTION_REQUIRED")
        selection = self._bridge.issued(selection_hash)
        if selection is None or not selection_routed(selection) or selection.selected_worker_id is None:
            raise CapabilityAdapterError("WORKER_ROUTER_EVIDENCE_UNRESOLVED")
        candidate = self._measured.candidate_for(selection.selected_worker_id)
        if candidate is None:
            raise CapabilityAdapterError("SELECTED_WORKER_NOT_A_MEASURED_LOCAL_PROFILE")
        items = [_prompt_package(context.artifacts.get(ref)) for ref in request.inputs]
        if len(items) != 1:
            raise CapabilityAdapterError("LOCAL_INFERENCE_REQUIRES_ONE_PROMPT_PACKAGE")
        run_id = self._run_ids()
        try:
            route = self._provider.route(RouteIntent(self._capability, request.classification, context.project_id,
                                                     profile_id=candidate.profile.profile_id))
            execution = self._provider.execute(route, run_id=run_id, work_package_id=request.stage_id, inputs=items,
                                               output_path=OUTPUT_PATH, allowed_paths=(OUTPUT_PATH,),
                                               timeout_seconds=self._timeout, max_output_tokens=self._max_tokens)
        except RoutingDenied as exc:
            self.executions.append({"run_id": run_id, "refused": exc.code, "considered": exc.considered})
            raise CapabilityAdapterError("LOCAL_ROUTE_DENIED") from None
        except NodeExecutionError as exc:
            self.executions.append({"run_id": run_id, "refused": exc.code, "failure": exc.failure})
            raise CapabilityAdapterError(exc.code.split(":", 1)[0][:64], retryable=exc.retryable) from None
        measured = route.measured
        if measured is None or measured.profile_id != candidate.profile.profile_id:
            raise CapabilityAdapterError("MEASURED_ROUTE_MISMATCH")
        output = context.artifacts.put(execution.artifact_bytes, media_type="text/plain", kind=self._output_kind)
        self.executions.append({"run_id": run_id, "worker_id": selection.selected_worker_id,
                                "route_worker_id": route.worker_id, "measured": measured,
                                "considered": route.considered, "record": execution.record,
                                "receipt": execution.receipt})
        record = execution.record
        metrics = {key: float(record[key]) for key in ("inference_ms", "queue_delay_ms")
                   if isinstance(record.get(key), (int, float))}
        return StageOutput(outputs=(output,), metrics=metrics, evidence={
            "profile_id": measured.profile_id, "manifest_digest": measured.manifest_digest,
            "binding_hash": measured.binding_hash, "requirement_hash": measured.requirement_hash,
            "node_id": route.registration.node_id, "job_id": str(record.get("job_id")),
            "node_receipt_hash": str(execution.receipt.get("receipt_hash")), "router_selection_hash": selection_hash})


def _prompt_package(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(payload)
    except ValueError:
        raise CapabilityAdapterError("PROMPT_PACKAGE_INVALID") from None
    messages = document.get("messages") if isinstance(document, dict) else None
    if not isinstance(messages, list) or not messages or not all(
            isinstance(m, dict) and m.get("role") in {"system", "user"} and isinstance(m.get("content"), str)
            for m in messages):
        raise CapabilityAdapterError("PROMPT_PACKAGE_INVALID")
    package: dict[str, Any] = {"messages": [{"role": m["role"], "content": m["content"]} for m in messages]}
    schema = document.get("response_schema")
    if schema is not None:
        if not isinstance(schema, dict) or not schema or len(json.dumps(schema)) > MAX_SCHEMA_CHARS:
            raise CapabilityAdapterError("PROMPT_PACKAGE_INVALID")
        package["response_schema"] = schema
    return package
