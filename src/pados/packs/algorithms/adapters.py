# SPDX-License-Identifier: AGPL-3.0-only
"""Algorithm adapters (CapabilityAdapter implementations run only via ``run_bound_stage``).

An adapter reads exactly one authorized ``algorithms.dataset`` artifact through the stage-scoped facade,
validates parameters against the algorithm's schema, runs the pure algorithm and writes two artifacts: the
deterministic result envelope and the reproducibility record (which alone carries runtime and compute
target). Everything is computed and validated before the first write, so a failure leaves no output
(FAIL_CLOSED). Outputs are candidates; nothing here commits, accepts or audits.

``execute`` is a closed-error boundary: an ``AlgorithmError`` becomes ``CapabilityAdapterError(code)``,
any other exception becomes ``ALGORITHM_UNEXPECTED_ERROR``, and both are raised OUTSIDE the handler so
neither ``__cause__`` nor ``__context__`` carries input content. Only the fabric's own
``ArtifactAccessDenied`` (a closed code from the stage facade) propagates unchanged.

Wall-clock guard: ``deadline_seconds`` (default ``DEFAULT_DEADLINE_SECONDS`` = 300 s, at most
``MAX_DEADLINE_SECONDS`` = 3600 s, ``None`` disables it) starts when ``execute`` starts and is polled by the
algorithm's work meter (``time.monotonic``). It can only ABORT a run (``ALGORITHM_DEADLINE_EXCEEDED``, no
output written); it never changes a result, so a completed result is deterministic. Whether a run
completes before the deadline depends on the host, which the record states (``completion``).
"""
from __future__ import annotations

import re
import time

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactAccessDenied
from pados.capability.model import AdapterDescriptor
from pados.capability.receipts import parameters_hash
from pados.lifecycle.minimization import assert_minimized
from pados.packs.algorithms.contracts import (
    DATASET_KIND,
    JSON_MEDIA,
    MAX_DATASET_BYTES,
    MAX_RESULT_BYTES,
    RECORD_KEYS,
    RECORD_KIND,
    RESULT_KIND,
    AlgorithmError,
    Completion,
    ReproducibilityRecord,
    ResultEnvelope,
    canonical_json,
    dataset_hash,
    no_credentials,
    parse_dataset,
    sha256_hex,
)
from pados.packs.algorithms.meter import DEFAULT_DEADLINE_SECONDS, optional_deadline, valid_deadline_seconds
from pados.packs.algorithms.registry import AlgorithmSpec, adapter_descriptor

_TARGET = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")


def _request_hash(request: StageRequest) -> str:
    """The shared ``parameters_hash`` (the value ``StageReceipt.parameters_hash`` and replay use)."""
    try:
        return parameters_hash(request.parameters)
    except (TypeError, ValueError, RecursionError):
        pass
    raise AlgorithmError("ALGORITHM_PARAMETERS_INVALID")


def _minimized_record(document: dict[str, object]) -> None:
    try:
        assert_minimized(document, allowed_keys=RECORD_KEYS)
        return
    except ValueError:
        pass
    raise AlgorithmError("ALGORITHM_RECORD_NOT_MINIMIZED")


class AlgorithmAdapter:
    """One deterministic algorithm as a capability adapter."""

    def __init__(self, spec: AlgorithmSpec, *, compute_target: str = "local-cpu",
                 deadline_seconds: float | None = DEFAULT_DEADLINE_SECONDS) -> None:
        if not _TARGET.fullmatch(compute_target):
            raise ValueError("COMPUTE_TARGET_INVALID")
        if deadline_seconds is not None and not valid_deadline_seconds(deadline_seconds):
            raise ValueError("ALGORITHM_DEADLINE_INVALID")
        self._spec = spec
        self._descriptor = adapter_descriptor(spec)
        self._target = compute_target
        self._deadline_seconds = None if deadline_seconds is None else float(deadline_seconds)

    @property
    def deadline_seconds(self) -> float | None:
        return self._deadline_seconds

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    @property
    def spec(self) -> AlgorithmSpec:
        return self._spec

    def available(self) -> bool:
        return self._spec.compute is not None

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        try:
            return self._execute(request, context)
        except ArtifactAccessDenied:
            raise
        except AlgorithmError as exc:
            code = exc.code
        except Exception:  # noqa: BLE001 - closed-error boundary
            code = "ALGORITHM_UNEXPECTED_ERROR"
        raise CapabilityAdapterError(code) from None  # outside the handler: empty __cause__ and __context__

    def _execute(self, request: StageRequest, context: StageContext) -> StageOutput:
        spec = self._spec
        deadline = optional_deadline(self._deadline_seconds)  # starts now; polled by the work meter
        if spec.compute is None:
            raise AlgorithmError("ALGORITHM_CONTRACT_ONLY")
        if request.capability_id != spec.capability.capability_id:
            raise AlgorithmError("ALGORITHM_CAPABILITY_MISMATCH")
        if request.seed is not None:
            raise AlgorithmError("ALGORITHM_SEED_NOT_APPLICABLE")  # DETERMINISTIC: a seed would be a false claim
        if len(request.inputs) != 1:
            raise AlgorithmError("ALGORITHM_INPUT_COUNT_INVALID")
        source = request.inputs[0]
        if source.kind != DATASET_KIND or source.media_type != JSON_MEDIA or source.size_bytes > MAX_DATASET_BYTES:
            raise AlgorithmError("ALGORITHM_INPUT_NOT_DATASET")
        dataset = parse_dataset(context.artifacts.get(source))
        request_hash = _request_hash(request)
        parameters = spec.parse_parameters(request.parameters)
        started = time.perf_counter_ns()
        result, metrics = spec.run(dataset, parameters, deadline=deadline)
        runtime_ms = (time.perf_counter_ns() - started) // 1_000_000
        envelope = ResultEnvelope(
            algorithm_id=spec.algorithm_id, algorithm_version=spec.algorithm_version,
            capability_id=spec.capability.capability_id, dataset_hash=dataset_hash(dataset),
            parameters_hash=spec.parameters_hash(parameters), seed=None,
            parameters=parameters.model_dump(mode="json"), result=result, metrics=metrics)
        payload = canonical_json(envelope.canonical())
        if len(payload) > MAX_RESULT_BYTES:
            raise AlgorithmError("ALGORITHM_RESULT_TOO_LARGE")
        no_credentials(envelope.canonical(), "ALGORITHM_RESULT_CREDENTIAL_SHAPED")
        record = ReproducibilityRecord(
            algorithm_id=spec.algorithm_id, algorithm_version=spec.algorithm_version,
            capability_id=spec.capability.capability_id, adapter_id=self._descriptor.adapter_id,
            parameters_hash=envelope.parameters_hash,
            request_parameters_hash=request_hash,
            dataset_hash=envelope.dataset_hash, dataset_artifact_sha256=source.sha256, seed=None,
            result_hash=sha256_hex(payload), metrics=metrics,
            compute_target=self._target, deadline_seconds=self._deadline_seconds,
            completion=Completion.DETERMINISTIC if deadline is None else Completion.NONDETERMINISTIC_DEADLINE)
        document = record.canonical()
        _minimized_record(document)
        result_ref = context.artifacts.put(payload, media_type=JSON_MEDIA, kind=RESULT_KIND)
        record_ref = context.artifacts.put(canonical_json(document), media_type=JSON_MEDIA, kind=RECORD_KIND)
        if result_ref.sha256 != record.result_hash:
            raise AlgorithmError("ALGORITHM_RESULT_HASH_MISMATCH")
        return StageOutput(
            outputs=(result_ref, record_ref),
            metrics={**metrics, "runtime_ms": float(runtime_ms)},
            evidence={"algorithm_id": spec.algorithm_id, "algorithm_version": spec.algorithm_version,
                      "result_sha256": result_ref.sha256, "record_sha256": record_ref.sha256,
                      "dataset_hash": envelope.dataset_hash, "parameters_hash": envelope.parameters_hash,
                      "compute_target": self._target})
