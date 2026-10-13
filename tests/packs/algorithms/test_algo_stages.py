"""Algorithm adapters through the trusted stage boundary: provenance, replay, fail-closed behaviour."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from _algo_harness import matrix, policy, put_json, run_bound, run_stage, stored_objects, students
from pados.capability.adapter import CapabilityAdapterError, StageBindingError, StageRequest, bind_stage
from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts
from pados.capability.model import Determinism, Distribution, LifecycleState, VerificationMethod
from pados.capability.policy import PolicyCode, evaluate
from pados.capability.receipts import StageReceipt, VerificationStatus, parameters_hash
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.lifecycle.minimization import assert_minimized
from pados.packs.algorithms import ALGORITHMS, AlgorithmAdapter, ExpectedStage, load_result, verify_replay
from pados.packs.algorithms.contracts import (
    RECORD_KEYS,
    RECORD_KIND,
    RESULT_KIND,
    AlgorithmError,
    Dataset,
    canonical_json,
    dataset_hash,
    load_record,
    parse_json_document,
)
from pados.packs.algorithms.registry import DEMATEL, DRSA, FCM

FCM_PARAMS = {"initial_state": {"A": 1, "B": 1}}


def _store(tmp_path: Path) -> ObjectStoreArtifacts:
    return ObjectStoreArtifacts(tmp_path / "objects")


def _run(store: ObjectStoreArtifacts, spec: Any, document: Any, parameters: dict[str, Any] | None = None,
         **kwargs: Any) -> tuple[ArtifactRef, Any]:
    source = put_json(store, document)
    return source, run_stage(store, spec.capability, AlgorithmAdapter(spec), [source], parameters, **kwargs)


def test_descriptors_are_deterministic_local_and_pados_owned() -> None:
    for spec in ALGORITHMS.values():
        descriptor = AlgorithmAdapter(spec).descriptor
        assert spec.capability.determinism is descriptor.determinism is Determinism.DETERMINISTIC
        assert descriptor.network_requirement is NetworkRequirement.LOCAL_ONLY
        assert descriptor.license.distribution is Distribution.NONE and not descriptor.delegates_to_worker_router
        assert descriptor.grant_capability == f"CAPABILITY:{spec.capability.capability_id}"
        if spec.compute is not None:
            assert spec.capability.lifecycle is LifecycleState.ACTIVE
            assert VerificationMethod.DETERMINISTIC_REPLAY in spec.capability.verification
        else:
            assert spec.capability.lifecycle is LifecycleState.EXPERIMENTAL
    assert {s.capability.capability_id for s in ALGORITHMS.values()} >= {
        "ROUGH_SET_RULE_LEARNING", "FUZZY_COGNITIVE_MAP", "MULTI_CRITERIA_DECISION_DEMATEL"}


@pytest.mark.parametrize("spec,document,parameters", [
    (DRSA, students(), {}),
    (FCM, matrix(("A", "B"), [[0, 0.5], [0, 0]]), FCM_PARAMS),
    (DEMATEL, matrix(("A", "B"), [[0, 3], [1, 0]]), {}),
], ids=["drsa", "fcm", "dematel"])
def test_stage_output_has_complete_provenance(tmp_path: Path, spec: Any, document: Any,
                                              parameters: dict[str, Any]) -> None:
    store = _store(tmp_path)
    source, output = _run(store, spec, document, parameters)
    result_ref, record_ref = output.outputs
    assert (result_ref.kind, record_ref.kind) == (RESULT_KIND, RECORD_KIND)
    assert output.semantic_authority == "NONE"
    envelope = load_result(store.get(result_ref), result_ref)
    record = load_record(store.get(record_ref))
    normalized = spec.parse_parameters(parameters)
    assert record.algorithm_id == envelope.algorithm_id == spec.algorithm_id
    assert record.algorithm_version == spec.algorithm_version
    assert record.capability_id == spec.capability.capability_id
    assert record.adapter_id == AlgorithmAdapter(spec).descriptor.adapter_id
    assert record.parameters_hash == envelope.parameters_hash == spec.parameters_hash(normalized)
    assert record.request_parameters_hash == parameters_hash(parameters)
    assert record.dataset_hash == envelope.dataset_hash == dataset_hash(Dataset.model_validate(document))
    assert record.dataset_artifact_sha256 == source.sha256
    assert record.seed is None and envelope.seed is None
    assert record.result_hash == result_ref.sha256
    assert record.metrics == envelope.metrics and record.compute_target == "local-cpu"
    assert "runtime_ms" not in json.loads(store.get(record_ref))  # timing is a stage metric, never an artifact
    assert set(record.canonical()) == RECORD_KEYS
    assert_minimized(record.canonical(), allowed_keys=RECORD_KEYS)
    assert output.evidence["result_sha256"] == result_ref.sha256
    assert output.evidence["record_sha256"] == record_ref.sha256
    assert "runtime_ms" not in json.loads(store.get(result_ref))  # result bytes carry no run-specific data


def test_determinism_and_encoding_independence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _, first = _run(store, DRSA, students())
    _, second = _run(store, DRSA, students(), stage_id="s2")
    reordered = json.loads(json.dumps(students()))
    reordered = dict(reversed(list(reordered.items())))
    source = put_json(store, reordered, indent=2)
    third = run_stage(store, DRSA.capability, AlgorithmAdapter(DRSA), [source], {}, stage_id="s3")
    assert first.outputs[0].sha256 == second.outputs[0].sha256 == third.outputs[0].sha256
    assert source.sha256 != put_json(store, students()).sha256  # different bytes, same canonical dataset


def test_deterministic_replay_passes_and_feeds_a_stage_receipt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = put_json(store, matrix(("A", "B"), [[0, 0.5], [0, 0]]))
    adapter = AlgorithmAdapter(FCM)
    binding, output = run_bound(store, FCM.capability, adapter, [source], FCM_PARAMS)
    result_ref, record_ref = output.outputs
    expected = ExpectedStage.from_binding(binding, output)
    verdict = verify_replay(expected, store.get(record_ref), store.get(result_ref), store.get(source), FCM_PARAMS)
    assert verdict.passed and verdict.code == "REPLAY_MATCH" and len(verdict.evidence_hash) == 64
    receipt = StageReceipt(
        plan_id="pl", stage_id="s1", attempt_id="a1", capability_id=FCM.capability.capability_id,
        capability_family=FCM.capability.family, capability_version=FCM.capability.version,
        capability_digest=binding.capability_digest, adapter_id=adapter.descriptor.adapter_id,
        adapter_version=adapter.descriptor.version, adapter_digest=binding.adapter_digest,
        resource_id="local-cpu", determinism=Determinism.DETERMINISTIC, inputs=(source,),
        outputs=output.outputs, parameters_hash=parameters_hash(FCM_PARAMS),
        started_at="2026-10-03T00:00:00+00:00", finished_at="2026-10-03T00:00:01+00:00", duration_ms=1000,
        verification_method=verdict.method, verification=verdict.status,
        verification_evidence_hash=verdict.evidence_hash, verifier_id=verdict.verifier_id,
        policy_decision_hash="b" * 64, prev_hash="0" * 64)
    assert receipt.verification is VerificationStatus.PASSED and receipt.semantic_authority == "NONE"
    # The receipt's own binding re-verifies the stored run.
    again = verify_replay(ExpectedStage.from_receipt(receipt), store.get(record_ref), store.get(result_ref),
                          store.get(source), FCM_PARAMS)
    assert again.passed and again.evidence_hash == verdict.evidence_hash


def test_replay_detects_tampering(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source = put_json(store, students())
    binding, output = run_bound(store, DRSA.capability, AlgorithmAdapter(DRSA), [source], {})
    expected = ExpectedStage.from_binding(binding, output)
    result_ref, record_ref = output.outputs
    record, result, dataset = store.get(record_ref), store.get(result_ref), store.get(source)
    assert verify_replay(expected, record, result + b" ", dataset, {}).code == "REPLAY_RESULT_HASH_MISMATCH"
    assert verify_replay(expected, record, result, dataset, {"max_rules": 10}).code == \
        "REPLAY_RECORD_REQUEST_PARAMETERS_HASH_MISMATCH"
    other = canonical_json(students() | {"decision": "Overall", "missing": "DROP_OBJECT"})
    assert verify_replay(expected, record, result, other, {}).code == "REPLAY_DATASET_ARTIFACT_MISMATCH"
    document = json.loads(record)
    stale = canonical_json({**document, "algorithm_version": "0"})
    assert verify_replay(expected, stale, result, dataset, {}).code == "REPLAY_ALGORITHM_VERSION_MISMATCH"
    # A forged result (rules changed) with a record that matches the forgery still fails, even when the
    # caller's output references were taken from the forged bytes: replay diverges.
    forged = json.loads(result)
    forged["result"]["rules"] = forged["result"]["rules"][:1]
    forged_bytes = canonical_json(forged)
    forged_record = canonical_json({**document, "result_hash": hashlib.sha256(forged_bytes).hexdigest()})
    assert verify_replay(expected, forged_record, forged_bytes, dataset, {}).code == "REPLAY_BINDING_OUTPUT_MISMATCH"
    laundered = replace(expected, outputs=(
        ArtifactRef(sha256=hashlib.sha256(forged_bytes).hexdigest(), media_type="application/json",
                    size_bytes=len(forged_bytes), kind=RESULT_KIND),
        ArtifactRef(sha256=hashlib.sha256(forged_record).hexdigest(), media_type="application/json",
                    size_bytes=len(forged_record), kind=RECORD_KIND)))
    verdict = verify_replay(laundered, forged_record, forged_bytes, dataset, {})
    assert verdict.code == "REPLAY_DIVERGED" and verdict.status is VerificationStatus.FAILED
    assert verify_replay(expected, b"{}", result, dataset, {}).code.startswith("REPLAY_RECORD")


@pytest.mark.parametrize("mutate,parameters,code", [
    (lambda d: d["rows"][0].__setitem__(0, None), {}, "DATASET_MISSING_VALUE"),
    (lambda d: d["rows"][0].__setitem__(0, "excellent"), {}, "DATASET_VALUE_OUT_OF_DOMAIN"),
    (lambda d: d.update(schema="other.v9"), {}, "DATASET_SCHEMA_UNSUPPORTED"),
    (lambda d: d["rows"].pop(), {}, "DATASET_OBJECTS_INVALID"),
    (lambda d: d["columns"][0].update(name="bad name"), {}, "DATASET_COLUMN_NAME_INVALID"),
    (lambda d: d.update(extra=1), {}, "DATASET_SCHEMA_INVALID"),
    (lambda d: None, {"unexpected": True}, "ALGORITHM_PARAMETERS_INVALID"),
    (lambda d: None, {"criteria": []}, "DRSA_CRITERIA_INVALID"),
    (lambda d: d.update(decision=None), {}, "DRSA_DECISION_REQUIRED"),
])
def test_invalid_datasets_and_parameters_fail_closed_without_output(tmp_path: Path, mutate: Any,
                                                                    parameters: dict[str, Any], code: str) -> None:
    store = _store(tmp_path)
    document = students()
    mutate(document)
    source = put_json(store, document)
    before = stored_objects(tmp_path / "objects")
    with pytest.raises(CapabilityAdapterError) as caught:
        run_stage(store, DRSA.capability, AlgorithmAdapter(DRSA), [source], parameters)
    assert caught.value.code == code
    assert stored_objects(tmp_path / "objects") == before  # FAIL_CLOSED: nothing written


def test_wrong_inputs_seed_and_capability_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    dataset = put_json(store, students())
    adapter = AlgorithmAdapter(DRSA)
    with pytest.raises(CapabilityAdapterError, match="ALGORITHM_SEED_NOT_APPLICABLE"):
        run_stage(store, DRSA.capability, adapter, [dataset], {}, seed=7)
    not_dataset = put_json(store, students(), kind="ocr.text")
    with pytest.raises(CapabilityAdapterError, match="ALGORITHM_INPUT_NOT_DATASET"):
        run_stage(store, DRSA.capability, adapter, [not_dataset], {})
    with pytest.raises(CapabilityAdapterError, match="ALGORITHM_INPUT_COUNT_INVALID"):
        run_stage(store, DRSA.capability, adapter, [dataset, put_json(store, matrix(("A", "B"), [[0, 1], [1, 0]]))])
    garbage = store.put(b"\xff\xfe not json", media_type="application/json", kind="algorithms.dataset")
    with pytest.raises(CapabilityAdapterError, match="DATASET_NOT_JSON"):
        run_stage(store, DRSA.capability, adapter, [garbage], {})
    duplicate = store.put(b'{"schema":"a","schema":"b"}', media_type="application/json", kind="algorithms.dataset")
    with pytest.raises(CapabilityAdapterError, match="JSON_DUPLICATE_KEY"):
        run_stage(store, DRSA.capability, adapter, [duplicate], {})
    nan = store.put(b'{"rows":[[NaN]]}', media_type="application/json", kind="algorithms.dataset")
    with pytest.raises(CapabilityAdapterError, match="JSON_NON_FINITE_NUMBER"):
        run_stage(store, DRSA.capability, adapter, [nan], {})


def test_credential_shaped_dataset_content_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    document = students()
    label = "s" + "k-" + "x" * 12  # assembled at runtime; never a literal credential in source
    document["columns"][0]["domain"] = ["bad", "medium", label]
    document["rows"][0][0] = label
    with pytest.raises(CapabilityAdapterError, match="DATASET_CREDENTIAL_SHAPED_CONTENT"):
        _run(store, DRSA, document)


def test_policy_still_gates_algorithm_stages(tmp_path: Path) -> None:
    store = _store(tmp_path)
    adapter = AlgorithmAdapter(DEMATEL)
    ctx, decision, _ = policy(DEMATEL.capability, adapter, granted=["MULTI_CRITERIA_DECISION_DEMATEL"])
    assert decision.allowed
    # A grant for another capability does not authorize this one (installed != authorized).
    unauthorized = replace(ctx, scope=replace(ctx.scope, capabilities=frozenset({"CAPABILITY:FUZZY_COGNITIVE_MAP"})))
    denied = evaluate(DEMATEL.capability, adapter.descriptor, unauthorized, adapter_available=adapter.available)
    assert denied.code is PolicyCode.NOT_AUTHORIZED
    source = put_json(store, matrix(("A", "B"), [[0, 3], [1, 0]]))
    request = StageRequest("s1", DEMATEL.capability.capability_id, (source,), {}, DataClassification.CONFIDENTIAL)
    with pytest.raises(StageBindingError, match="NOT_ALLOWED"):
        bind_stage(DEMATEL.capability, adapter.descriptor, unauthorized, denied, request,
                   permitted_inputs=frozenset({source.sha256}))


def test_json_parser_bounds() -> None:
    with pytest.raises(AlgorithmError, match="DATASET_TOO_LARGE"):
        parse_json_document(b" " * 11, max_bytes=10, code="DATASET")
    with pytest.raises(AlgorithmError, match="JSON_TOO_DEEP"):
        parse_json_document(b'{"a":' + b"[" * 40 + b"]" * 40 + b"}", max_bytes=1000, code="DATASET")
    with pytest.raises(AlgorithmError, match="DATASET_NOT_OBJECT"):
        parse_json_document(b"[1]", max_bytes=1000, code="DATASET")


def test_dataset_size_bounds() -> None:
    columns = [{"name": f"c{i}", "type": "NUMERIC"} for i in range(65)]
    with pytest.raises(ValueError, match="DATASET_COLUMNS_INVALID"):
        Dataset.model_validate({"columns": columns, "object_ids": ["a"], "rows": [[0.0] * 65]})
    columns = [{"name": f"c{i}", "type": "NUMERIC"} for i in range(50)]
    rows = [[0.0] * 50 for _ in range(2001)]
    with pytest.raises(ValueError, match="DATASET_TOO_LARGE"):
        Dataset.model_validate({"columns": columns, "object_ids": [f"o{i}" for i in range(2001)], "rows": rows})
