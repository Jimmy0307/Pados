# SPDX-License-Identifier: AGPL-3.0-only
"""Local chat provider that executes ONLY under a measured execution binding.

There is no unbound request shape here: the provider is constructed from a ``BoundExecution`` and runs through
the existing compute-node backend adapter (``pados.compute_node.backends.OllamaBackend``), which is the one place
that translates the generic ``ExecutionConfig`` into runtime request fields. The backend's echo of the requested
configuration must equal the bound one, otherwise the call is refused (``MEASURED_EXECUTION_CONFIG_NOT_APPLIED``).
"""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from pados.compute_node.backends import BackendError, InferenceBackend, OllamaBackend
from pados.compute_node.manifest import NodeCapability
from pados.providers.execution_binding import (
    MEASURED_EXECUTION_CONFIG_MISMATCH,
    MEASURED_EXECUTION_CONFIG_NOT_APPLIED,
    BoundExecution,
    refuse,
)
from pados.resources.capability_profile import ExecutionConfig, OutputConstraint

_RUNTIME_METRICS = ("done_reason", "eval_count", "prompt_eval_count", "total_duration", "response_schema_sha256")
BACKEND_TIMEOUT = "BACKEND_TIMEOUT"


class MeasuredExecutionFailed(RuntimeError):
    """The bound call ran and failed with a stable backend code (never runtime or model text)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MeasuredExecutionTimeout(TimeoutError):
    """The bound call exceeded ``timeout_seconds`` (mapped to the cognitive TIMEOUT failure)."""


class MeasuredChatProvider:
    def __init__(self, bound: BoundExecution, *, backend: InferenceBackend | None = None) -> None:
        self.bound = bound
        self.backend: InferenceBackend = backend or OllamaBackend("pados-chat", bound.endpoint)
        self.last_evidence: dict[str, Any] | None = None

    def _item(self, prompt: str, response_schema: Mapping[str, Any] | None) -> dict[str, Any]:
        constrained = self.bound.execution.output_constraint is OutputConstraint.JSON_SCHEMA
        if constrained != bool(response_schema):  # never send unconstrained what was measured constrained
            raise refuse(MEASURED_EXECUTION_CONFIG_MISMATCH, ("output_constraint",))
        return {"messages": [{"role": "user", "content": prompt}],
                "response_schema": dict(response_schema) if response_schema else None}

    def _verify_applied(self, runtime: Mapping[str, Any]) -> None:
        try:
            requested = ExecutionConfig.model_validate(runtime.get("execution_requested"))
        except ValidationError:
            raise refuse(MEASURED_EXECUTION_CONFIG_NOT_APPLIED) from None
        if requested != self.bound.execution:
            raise refuse(MEASURED_EXECUTION_CONFIG_NOT_APPLIED)

    async def chat(self, prompt: str, *, response_schema: Mapping[str, Any] | None = None) -> str:
        item = self._item(prompt, response_schema)
        self.last_evidence = None
        try:
            output = await asyncio.to_thread(
                self.backend.run, NodeCapability.LOCAL_INFERENCE, self.bound.runtime_ref, item,
                max_output_tokens=self.bound.execution.max_output_tokens,
                timeout_seconds=float(self.bound.timeout_seconds), execution=self.bound.execution)
        except BackendError as exc:
            self.last_evidence = {**self.bound.evidence(), "error_code": exc.code, "runtime": dict(exc.detail)}
            if exc.code == BACKEND_TIMEOUT:
                raise MeasuredExecutionTimeout(exc.code) from None
            raise MeasuredExecutionFailed(exc.code) from None
        self._verify_applied(output.runtime)
        metrics = {key: output.runtime[key] for key in _RUNTIME_METRICS if key in output.runtime}
        self.last_evidence = {**self.bound.evidence(), "runtime": metrics}
        return output.content

    async def healthcheck(self) -> dict[str, str]:
        healthy = await asyncio.to_thread(self.backend.health)
        return {"status": "ok" if healthy else "unavailable", "provider": "measured-local"}

    async def estimate_context(self, prompt: str) -> int:
        return max(1, len(prompt.split()))
