# SPDX-License-Identifier: AGPL-3.0-only
"""Node-local job lifecycle and resource bounds.

Local execution state only: QUEUED -> RUNNING -> SUCCEEDED | FAILED |
CANCELLED | TIMED_OUT. The node manages only its own capacity (concurrency,
queue depth, timeout, input budget); it never schedules work elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pados.compute_node.artifacts import ArtifactStore
from pados.compute_node.backends import BackendError, InferenceBackend
from pados.compute_node.identity import record_mac
from pados.compute_node.manifest import (
    NODE_RUNTIME_VERSION,
    ModelAvailability,
    ModelManifest,
    NodeCapability,
    NodeLimits,
    NodeLoad,
)
from pados.enterprise.classification import DataClassification
from pados.resources.capability_profile import ExecutionConfig


class JobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


TERMINAL = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.TIMED_OUT})


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1, max_length=120)
    work_package_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=80)
    capability: NodeCapability
    model_id: str = Field(min_length=1, max_length=300)
    classification: DataClassification
    inputs: list[dict[str, Any]] = Field(min_length=1)
    output_path: str = Field(min_length=1, max_length=240)
    max_output_tokens: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0)
    # The measured execution configuration the caller bound (generic; the backend translates or refuses it).
    execution: ExecutionConfig | None = None

    @property
    def output_budget(self) -> int | None:
        return self.execution.max_output_tokens if self.execution is not None else self.max_output_tokens


class NodeRejection(RuntimeError):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Job:
    job_id: str
    request: ExecuteRequest
    model: ModelManifest
    created_at: str
    created_monotonic: float
    input_sha256: str
    manifest_hash: str
    state: JobState = JobState.QUEUED
    started_at: str | None = None
    finished_at: str | None = None
    queue_delay_ms: float | None = None
    inference_ms: float | None = None
    error_code: str | None = None
    error_detail: dict[str, Any] = field(default_factory=dict)  # bounded counters/labels only (BackendError.detail)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    runtime: dict[str, Any] = field(default_factory=dict)


class JobManager:
    def __init__(
        self, *, node_id: str, limits: NodeLimits, store: ArtifactStore,
        backends: Mapping[str, InferenceBackend], credential: str,
        models: Callable[[], list[ModelManifest]], manifest_hash: Callable[[], str],
        privacy_class: DataClassification, capabilities: frozenset[NodeCapability],
    ) -> None:
        self.node_id = node_id
        self.limits = limits
        self.store = store
        self.backends = dict(backends)
        self._credential = credential
        self._models = models
        self._manifest_hash = manifest_hash
        self.privacy_class = privacy_class
        self.capabilities = capabilities
        self._jobs: dict[str, Job] = {}
        self._by_request: dict[str, str] = {}
        self._lock = threading.RLock()
        # Admitted jobs whose worker call has not yet returned. Capacity is reserved here at admission, not
        # inferred from job states: a worker thread that has not been scheduled yet (QUEUED) or is still busy
        # after a cancel/timeout (terminal state, live thread) occupies a slot all the same.
        self._occupied = 0
        self._executor = ThreadPoolExecutor(max_workers=limits.max_concurrent_jobs, thread_name_prefix="pados-lcn")

    # -- admission -----------------------------------------------------------
    def submit(self, request: ExecuteRequest) -> dict[str, Any]:
        with self._lock:
            replay = self._replay_unlocked(request)
        if replay is not None:
            return self._record(replay)
        if request.capability not in self.capabilities:
            raise NodeRejection("CAPABILITY_NOT_ENABLED", 403)
        if not self.privacy_class.permits(request.classification):
            raise NodeRejection("CLASSIFICATION_ABOVE_NODE_CEILING", 403)
        model = next((m for m in self._models() if m.model_id == request.model_id), None)
        if model is None:
            raise NodeRejection("MODEL_NOT_ADVERTISED", 404)
        if model.availability is not ModelAvailability.AVAILABLE:
            raise NodeRejection(f"MODEL_{model.availability.value}", 409)
        if request.capability not in model.tasks:
            raise NodeRejection("MODEL_CAPABILITY_MISMATCH", 400)
        if not model.privacy_class.permits(request.classification):
            raise NodeRejection("CLASSIFICATION_ABOVE_MODEL_CEILING", 403)
        if len(request.inputs) > self.limits.max_batch_items:
            raise NodeRejection("BATCH_TOO_LARGE", 413)
        encoded = json.dumps(request.inputs, sort_keys=True, ensure_ascii=False)
        if len(encoded) > self.limits.max_input_chars:
            raise NodeRejection("INPUT_BUDGET_EXCEEDED", 413)
        if (request.timeout_seconds or 0) > self.limits.max_timeout_seconds:
            raise NodeRejection("TIMEOUT_ABOVE_NODE_LIMIT", 400)
        if request.execution is not None and request.max_output_tokens is not None \
                and request.max_output_tokens != request.execution.max_output_tokens:
            raise NodeRejection("EXECUTION_CONFIG_BUDGET_CONFLICT", 400)
        if (request.output_budget or 0) > self.limits.max_output_tokens:
            raise NodeRejection("OUTPUT_BUDGET_EXCEEDED", 413)
        with self._lock:
            # Idempotency and admission decide under one lock: a concurrent duplicate either replays the job
            # admitted first or is refused; it can never be acknowledged without its own reservation.
            replay = self._replay_unlocked(request)
            if replay is not None:
                return self._record(replay)
            if self._load_unlocked().saturated:
                raise NodeRejection("QUEUE_FULL", 429)
            self._occupied += 1
            job = Job(job_id=f"job-{uuid.uuid4()}", request=request, model=model, created_at=_now(),
                      created_monotonic=time.monotonic(),
                      input_sha256=hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                      manifest_hash=self._manifest_hash())
            self._jobs[job.job_id] = job
            self._by_request[request.request_id] = job.job_id
        try:
            self._executor.submit(self._occupy, job.job_id)
        except BaseException:
            self._release()
            raise
        return self._record(job)

    def _replay_unlocked(self, request: ExecuteRequest) -> Job | None:
        """The job already admitted for this request id, if any. Replaying is allowed only for the identical
        request: a different request reusing the id is refused instead of being answered with another job."""
        existing = self._by_request.get(request.request_id)
        if existing is None:
            return None
        job = self._jobs[existing]
        if job.request != request:
            raise NodeRejection("REQUEST_ID_CONFLICT", 409)
        return job

    def _occupy(self, job_id: str) -> None:
        try:
            self._run(job_id)
        finally:
            self._release()

    def _release(self) -> None:
        with self._lock:
            self._occupied -= 1

    # -- lifecycle -------------------------------------------------------------
    def _run(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.state is not JobState.QUEUED:
                return
            job.state = JobState.RUNNING
            job.started_at = _now()
            job.queue_delay_ms = round((time.monotonic() - job.created_monotonic) * 1000, 3)
        timeout = job.request.timeout_seconds or self.limits.default_timeout_seconds
        timer = threading.Timer(timeout, self._expire, args=(job_id,))
        timer.daemon = True
        timer.start()
        started = time.monotonic()
        try:
            backend = self.backends[job.model.backend]
            # ``execution`` is passed only when bound, so a backend without measured-configuration support keeps
            # working for unbound jobs and fails (closed) for bound ones.
            bound: dict[str, Any] = {} if job.request.execution is None else {"execution": job.request.execution}
            outputs = [backend.run(job.request.capability, job.model.backend_model, item,
                                   max_output_tokens=job.request.output_budget or self.limits.max_output_tokens,
                                   timeout_seconds=timeout, **bound) for item in job.request.inputs]
        except BackendError as exc:
            self._finish(job_id, JobState.FAILED, error=exc.code, detail=exc.detail)
            return
        except TypeError:
            # a backend without measured-configuration support cannot run a bound job (fail closed, named code)
            self._finish(job_id, JobState.FAILED, error="EXECUTION_CONFIG_UNSUPPORTED_BY_BACKEND"
                         if job.request.execution is not None else "NODE_INTERNAL_ERROR")
            return
        except Exception:  # noqa: BLE001 - never leak backend text; the code is enough
            self._finish(job_id, JobState.FAILED, error="NODE_INTERNAL_ERROR")
            return
        finally:
            timer.cancel()
        elapsed = round((time.monotonic() - started) * 1000, 3)
        if len(outputs) == 1:
            payload = outputs[0].content.encode("utf-8")
            media = "text/markdown" if job.request.output_path.endswith(".md") else (
                "application/json" if job.request.capability is not NodeCapability.LOCAL_INFERENCE else "text/plain")
        else:
            payload = "".join(json.dumps({"index": i, "output": o.content}, ensure_ascii=False) + "\n"
                              for i, o in enumerate(outputs)).encode("utf-8")
            media = "application/x-ndjson"
        with self._lock:
            if job.state is not JobState.RUNNING:
                return  # cancelled or timed out: the late result is discarded
            ref = self.store.put(payload, media_type=media, path=job.request.output_path)
            job.artifacts = [ref.as_dict()]
            job.runtime = {**outputs[0].runtime, "items": len(outputs)}
            job.inference_ms = elapsed
            job.state = JobState.SUCCEEDED
            job.finished_at = _now()

    def _expire(self, job_id: str) -> None:
        self._finish(job_id, JobState.TIMED_OUT, error="JOB_TIMEOUT")

    def _finish(self, job_id: str, state: JobState, *, error: str | None = None,
                detail: dict[str, Any] | None = None) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job.state in TERMINAL:
                return
            job.state = state
            job.error_code = error
            job.error_detail = dict(detail or {})
            job.finished_at = _now()

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise NodeRejection("JOB_NOT_FOUND", 404)
        self._finish(job_id, JobState.CANCELLED, error="CANCELLED_BY_REQUEST")
        return self._record(job)

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise NodeRejection("JOB_NOT_FOUND", 404)
        return self._record(job)

    def load(self) -> NodeLoad:
        with self._lock:
            return self._load_unlocked()

    def _load_unlocked(self) -> NodeLoad:
        """Slot occupancy: ``running`` counts held worker slots, ``queued`` the reservations waiting for one."""
        running = min(self._occupied, self.limits.max_concurrent_jobs)
        return NodeLoad(running=running, queued=self._occupied - running,
                        max_concurrent_jobs=self.limits.max_concurrent_jobs,
                        max_queue_depth=self.limits.max_queue_depth)

    def _record(self, job: Job) -> dict[str, Any]:
        with self._lock:
            record: dict[str, Any] = {
                "job_id": job.job_id, "node_id": self.node_id, "state": job.state.value,
                "request_id": job.request.request_id, "work_package_id": job.request.work_package_id,
                "run_id": job.request.run_id, "capability": job.request.capability.value,
                "model_id": job.model.model_id, "classification": job.request.classification.value,
                "input_sha256": job.input_sha256, "input_items": len(job.request.inputs),
                "output_path": job.request.output_path, "created_at": job.created_at,
                "started_at": job.started_at, "finished_at": job.finished_at,
                "queue_delay_ms": job.queue_delay_ms, "inference_ms": job.inference_ms,
                "error_code": job.error_code, "error_detail": dict(job.error_detail),
                "execution": None if job.request.execution is None else job.request.execution.model_dump(mode="json"),
                "artifacts": list(job.artifacts), "runtime": dict(job.runtime),
                "manifest_hash": job.manifest_hash, "node_runtime_version": NODE_RUNTIME_VERSION,
            }
        record["record_mac"] = record_mac(self._credential, record)
        return record

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
