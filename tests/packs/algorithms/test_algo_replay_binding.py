"""DETERMINISTIC_REPLAY is bound to the trusted caller's stage binding (CR-14 H-01).

Replay recomputes the run, and it also checks every field that a forged result/record pair could
otherwise falsify: the capability id and digest, the adapter id, version and digest, the request
parameters hash, the input and output artifact references, the seed, and the metrics. Each forged field
gets its own closed code.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import pados.packs.algorithms.replay as replay
from _algo_harness import matrix, put_json, run_bound, students
from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts
from pados.capability.model import Determinism
from pados.capability.receipts import StageReceipt, VerificationStatus, parameters_hash
from pados.packs.algorithms import AlgorithmAdapter
from pados.packs.algorithms.contracts import JSON_MEDIA, RECORD_KIND, RESULT_KIND, canonical_json
from pados.packs.algorithms.registry import DEMATEL, DRSA, FCM

FCM_PARAMS = {"initial_state": {"A": 1, "B": 1}}


class Run:
    def __init__(self, tmp_path: Path, spec: Any = DRSA, document: Any = None,
                 parameters: dict[str, Any] | None = None) -> None:
        self.store = ObjectStoreArtifacts(tmp_path / "objects")
        self.spec = spec
        self.parameters = parameters or {}
        self.source = put_json(self.store, students() if document is None else document)
        self.adapter = AlgorithmAdapter(spec)
        self.binding, self.output = run_bound(self.store, spec.capability, self.adapter, [self.source],
                                              self.parameters)
        self.result_ref, self.record_ref = self.output.outputs
        self.result = self.store.get(self.result_ref)
        self.record = self.store.get(self.record_ref)
        self.dataset = self.store.get(self.source)

    def expected(self) -> Any:
        return replay.ExpectedStage.from_binding(self.binding, self.output)

    def verify(self, expected: Any = None, *, record: bytes | None = None, result: bytes | None = None,
               parameters: Mapping[str, Any] | None = None) -> Any:
        return replay.verify_replay(self.expected() if expected is None else expected,
                                    self.record if record is None else record,
                                    self.result if result is None else result, self.dataset,
                                    self.parameters if parameters is None else parameters)


def _ref(payload: bytes, kind: str) -> ArtifactRef:
    return ArtifactRef(sha256=hashlib.sha256(payload).hexdigest(), media_type=JSON_MEDIA, size_bytes=len(payload),
                       kind=kind)


def _forge(run: Run, record_changes: dict[str, Any], result_changes: dict[str, Any] | None = None
           ) -> tuple[Any, bytes, bytes]:
    """A self-consistent forged pair, and a binding whose output refs were (wrongly) taken from the forgery,
    so that only the forged field itself can be detected."""
    result = canonical_json({**json.loads(run.result), **(result_changes or {})})
    record = canonical_json({**json.loads(run.record), "result_hash": hashlib.sha256(result).hexdigest(),
                             **record_changes})
    expected = replace(run.expected(), outputs=(_ref(result, RESULT_KIND), _ref(record, RECORD_KIND)))
    return expected, record, result


@pytest.mark.parametrize("spec,document,parameters", [
    (DRSA, students(), {}),
    (FCM, matrix(("A", "B"), [[0, 0.5], [0, 0]]), FCM_PARAMS),
    (DEMATEL, matrix(("A", "B"), [[0, 3], [1, 0]]), {}),
], ids=["drsa", "fcm", "dematel"])
def test_valid_run_matches_and_feeds_a_passed_stage_receipt(tmp_path: Path, spec: Any, document: Any,
                                                            parameters: dict[str, Any]) -> None:
    run = Run(tmp_path, spec, document, parameters)
    verdict = run.verify()
    assert verdict.code == "REPLAY_MATCH" and verdict.passed
    descriptor = run.adapter.descriptor
    receipt = StageReceipt(
        plan_id="pl", stage_id="s1", attempt_id="a1", capability_id=spec.capability.capability_id,
        capability_family=spec.capability.family, capability_version=spec.capability.version,
        capability_digest=run.binding.capability_digest, adapter_id=descriptor.adapter_id,
        adapter_version=descriptor.version, adapter_digest=run.binding.adapter_digest, resource_id="local-cpu",
        determinism=Determinism.DETERMINISTIC, inputs=(run.source,), outputs=run.output.outputs,
        parameters_hash=parameters_hash(parameters), started_at="2026-10-03T00:00:00+00:00",
        finished_at="2026-10-03T00:00:01+00:00", duration_ms=1000, verification_method=verdict.method,
        verification=verdict.status, verification_evidence_hash=verdict.evidence_hash,
        verifier_id=verdict.verifier_id, policy_decision_hash="b" * 64, prev_hash="0" * 64)
    assert receipt.verification is VerificationStatus.PASSED
    assert run.verify(replay.ExpectedStage.from_receipt(receipt)).code == "REPLAY_MATCH"


def _binding_forgeries(run: Run) -> Iterator[tuple[Any, str]]:
    expected = run.expected()
    other_dataset = canonical_json(students() | {"missing": "DROP_OBJECT"})
    yield replace(expected, capability_id="FUZZY_COGNITIVE_MAP"), "REPLAY_BINDING_CAPABILITY_MISMATCH"
    yield replace(expected, capability_digest="c" * 64), "REPLAY_BINDING_CAPABILITY_DIGEST_MISMATCH"
    yield replace(expected, adapter_id="algorithms.fcm.inference"), "REPLAY_BINDING_ADAPTER_MISMATCH"
    yield replace(expected, adapter_version="0"), "REPLAY_BINDING_ADAPTER_VERSION_MISMATCH"
    yield replace(expected, adapter_digest="d" * 64), "REPLAY_BINDING_ADAPTER_DIGEST_MISMATCH"
    yield replace(expected, request_parameters_hash=parameters_hash({"max_rules": 1})), \
        "REPLAY_BINDING_PARAMETERS_HASH_MISMATCH"
    yield replace(expected, inputs=(_ref(other_dataset, "algorithms.dataset"),)), "REPLAY_BINDING_INPUT_MISMATCH"
    yield replace(expected, inputs=(run.source, run.source)), "REPLAY_BINDING_INPUT_MISMATCH"
    yield replace(expected, inputs=(run.source.model_copy(update={"kind": "ocr.text"}),)), \
        "REPLAY_BINDING_INPUT_MISMATCH"
    yield replace(expected, outputs=(run.record_ref, run.result_ref)), "REPLAY_BINDING_OUTPUT_MISMATCH"
    yield replace(expected, outputs=(run.result_ref,)), "REPLAY_BINDING_OUTPUT_MISMATCH"


def test_each_forged_binding_field_has_its_own_code(tmp_path: Path) -> None:
    run = Run(tmp_path)
    seen = []
    for expected, code in _binding_forgeries(run):
        verdict = run.verify(expected)
        assert (verdict.code, verdict.status) == (code, VerificationStatus.FAILED), code
        seen.append(code)
    assert len(set(seen)) == 8


def test_each_forged_record_field_has_its_own_code(tmp_path: Path) -> None:
    run = Run(tmp_path)
    metrics = json.loads(run.record)["metrics"]
    cases = [
        ({"capability_id": "FUZZY_COGNITIVE_MAP"}, None, "REPLAY_RECORD_CAPABILITY_MISMATCH"),
        ({"adapter_id": "algorithms.fcm.inference"}, None, "REPLAY_RECORD_ADAPTER_MISMATCH"),
        ({"request_parameters_hash": parameters_hash({"max_rules": 1})}, None,
         "REPLAY_RECORD_REQUEST_PARAMETERS_HASH_MISMATCH"),
        ({"metrics": {**metrics, "rule_count": 1.0}}, None, "REPLAY_METRICS_MISMATCH"),
        ({"parameters_hash": "e" * 64}, None, "REPLAY_PARAMETERS_HASH_MISMATCH"),
        ({"dataset_hash": "f" * 64}, None, "REPLAY_DATASET_HASH_MISMATCH"),
    ]
    for record_changes, result_changes, code in cases:
        expected, record, result = _forge(run, record_changes, result_changes)
        verdict = run.verify(expected, record=record, result=result)
        assert (verdict.code, verdict.status) == (code, VerificationStatus.FAILED), code


def test_a_run_the_adapter_would_refuse_never_matches(tmp_path: Path) -> None:
    """Execution refuses every non-null seed; a self-consistent seeded pair used to replay as a MATCH."""
    run = Run(tmp_path)
    expected, record, result = _forge(run, {"seed": 7}, {"seed": 7})
    verdict = run.verify(expected, record=record, result=result)
    assert verdict.code == "REPLAY_SEED_NOT_APPLICABLE" and not verdict.passed


def test_falsified_request_provenance_never_matches(tmp_path: Path) -> None:
    """A record claiming another request (e.g. to launder an unauthorized parameter set) is refused, both
    against the recomputed request hash and against the caller's binding."""
    run = Run(tmp_path)
    claimed = parameters_hash({"criteria": ["M"]})
    expected, record, result = _forge(run, {"request_parameters_hash": claimed})
    assert run.verify(expected, record=record, result=result).code == \
        "REPLAY_RECORD_REQUEST_PARAMETERS_HASH_MISMATCH"
    # Same normalised parameters, different request (explicit default): bound to what was actually requested.
    explicit = {"max_rules": 500}
    assert run.verify(parameters=explicit).code == "REPLAY_RECORD_REQUEST_PARAMETERS_HASH_MISMATCH"


def test_record_and_result_tampering_still_detected(tmp_path: Path) -> None:
    run = Run(tmp_path)
    assert run.verify(result=run.result + b" ").code == "REPLAY_RESULT_HASH_MISMATCH"
    forged = json.loads(run.result)
    forged["result"]["rules"] = forged["result"]["rules"][:1]
    expected, record, result = _forge(run, {}, {"result": forged["result"]})
    assert run.verify(expected, record=record, result=result).code == "REPLAY_DIVERGED"
    # Re-encoded record bytes (still valid JSON) are not the bytes the stage wrote.
    assert run.verify(record=run.record + b" ").code == "REPLAY_BINDING_OUTPUT_MISMATCH"


class _ExplodingParameters(Mapping[str, Any]):
    def __getitem__(self, key: str) -> Any:
        raise RuntimeError("zz-marker-content")

    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("zz-marker-content")

    def __len__(self) -> int:
        return 1


def test_replay_never_raises_and_never_echoes(tmp_path: Path) -> None:
    run = Run(tmp_path)
    verdict = run.verify(parameters=_ExplodingParameters())
    assert verdict.code == "REPLAY_UNEXPECTED_ERROR" and verdict.status is VerificationStatus.FAILED
    assert "zz-marker" not in repr(verdict)
