"""An LLM (or any explainer) can describe a result but can never replace or alter the result artifact."""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from _algo_harness import policy, put_json, run_bound, run_stage, students
from pados.capability.adapter import CapabilityAdapterError, StageBindingError
from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts
from pados.capability.model import CapabilityFamily, Determinism
from pados.capability.policy import PolicyCode
from pados.enterprise.classification import DataClassification
from pados.packs.algorithms import AlgorithmAdapter, ExpectedStage, load_result, verify_replay
from pados.packs.algorithms.contracts import RESULT_KIND, AlgorithmError
from pados.packs.algorithms.explanation import (
    EXPLANATION_KIND,
    TEMPLATE_CAPABILITY,
    ModelExplanationAdapter,
    MAX_TEXT,
    TemplateExplanationAdapter,
    explanation_capability,
    render_template,
)
from pados.packs.algorithms.registry import DRSA

ROUTER_HASH = "a" * 64
MODEL = explanation_capability()


class HostileGenerator:
    """Pretends to be the algorithm: returns a forged 'result' and tries to mutate the facts it is given."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    def generate(self, facts: Mapping[str, Any], *, router_selection_hash: str) -> str:
        self.seen.append(dict(facts))
        if isinstance(facts, dict):
            facts["metrics"]["quality_of_approximation"] = 1.0  # type: ignore[index]
            facts["result_sha256"] = "f" * 64
        forged = {"schema": "pados.algorithms.result.v1", "result": {"rules": [], "quality_of_approximation": 1.0}}
        return json.dumps(forged)


class FixedGenerator:
    def __init__(self, text: str) -> None:
        self.text = text

    def generate(self, facts: Mapping[str, Any], *, router_selection_hash: str) -> str:
        return self.text


EXPECTED: dict[str, ExpectedStage] = {}


def _result(tmp_path: Path) -> tuple[ObjectStoreArtifacts, ArtifactRef, ArtifactRef, ArtifactRef, bytes]:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    source = put_json(store, students())
    binding, output = run_bound(store, DRSA.capability, AlgorithmAdapter(DRSA), [source], {})
    result_ref, record_ref = output.outputs
    EXPECTED[result_ref.sha256] = ExpectedStage.from_binding(binding, output)
    return store, source, result_ref, record_ref, store.get(result_ref)


def _model_stage(store: ObjectStoreArtifacts, adapter: ModelExplanationAdapter, inputs: list[ArtifactRef],
                 parameters: dict[str, Any] | None = None, router: str | None = ROUTER_HASH) -> Any:
    return run_stage(store, MODEL, adapter, inputs, parameters, classification=DataClassification.INTERNAL,
                     router_selection_hash=router)


def test_contract_shapes() -> None:
    assert MODEL.family is CapabilityFamily.MODEL_RUNTIME and MODEL.determinism is Determinism.PROBABILISTIC
    model = ModelExplanationAdapter(FixedGenerator("x"))
    assert model.descriptor.delegates_to_worker_router and model.descriptor.capability_id == MODEL.capability_id
    assert TEMPLATE_CAPABILITY.determinism is Determinism.DETERMINISTIC
    assert ModelExplanationAdapter().available() is False  # no generator wired -> never selected
    _, decision, _ = policy(MODEL, ModelExplanationAdapter(), classification=DataClassification.INTERNAL)
    assert decision.code is PolicyCode.ADAPTER_UNAVAILABLE


def test_template_explanation_is_a_separate_labelled_artifact(tmp_path: Path) -> None:
    store, _, result_ref, _, original = _result(tmp_path)
    first = run_stage(store, TEMPLATE_CAPABILITY, TemplateExplanationAdapter(), [result_ref])
    second = run_stage(store, TEMPLATE_CAPABILITY, TemplateExplanationAdapter(), [result_ref], stage_id="s2")
    (ref,) = first.outputs
    assert ref.kind == EXPLANATION_KIND and ref.sha256 == second.outputs[0].sha256  # deterministic
    document = json.loads(store.get(ref))
    assert document["label"] == "EXPLANATION" and document["authoritative"] is False
    assert document["semantic_authority"] == "NONE" and document["source_result_sha256"] == result_ref.sha256
    assert "if M >= medium and L >= medium then AT_LEAST medium (support 4)" in document["text"]
    assert store.get(result_ref) == original
    with pytest.raises(AlgorithmError, match="RESULT_ARTIFACT_KIND_INVALID"):
        load_result(store.get(ref), ref)
    spoofed = ArtifactRef(sha256=ref.sha256, media_type=ref.media_type, size_bytes=ref.size_bytes, kind=RESULT_KIND)
    with pytest.raises(AlgorithmError):
        load_result(store.get(ref), spoofed)  # even a relabelled reference is not a valid result


def test_model_explanation_cannot_replace_or_alter_the_result(tmp_path: Path) -> None:
    store, source, result_ref, record_ref, original = _result(tmp_path)
    generator = HostileGenerator()
    output = _model_stage(store, ModelExplanationAdapter(generator), [result_ref])
    (ref,) = output.outputs
    assert ref.kind == EXPLANATION_KIND and ref.sha256 != result_ref.sha256
    document = json.loads(store.get(ref))
    assert document["explainer"] == "MODEL" and document["router_selection_hash"] == ROUTER_HASH
    assert document["source_result_sha256"] == result_ref.sha256  # the generator's mutation did not leak
    assert json.loads(document["text"])["result"]["rules"] == []  # forged 'result' is only quoted prose
    # The result artifact is byte-identical, still loads, and still replays.
    assert store.get(result_ref) == original
    assert load_result(original, result_ref).result["quality_of_approximation"] == 0.75
    assert verify_replay(EXPECTED[result_ref.sha256], store.get(record_ref), original, store.get(source), {}).passed
    # Consumers asking for a result never accept the explanation instead.
    with pytest.raises(AlgorithmError, match="RESULT_ARTIFACT_KIND_INVALID"):
        load_result(store.get(ref), ref)
    # The model only ever saw facts derived from the result (no dataset rows).
    assert set(generator.seen[0]) == {"algorithm_id", "algorithm_version", "capability_id", "result_sha256",
                                      "metrics", "highlights"}


def test_model_stage_requires_worker_router_evidence(tmp_path: Path) -> None:
    store, _, result_ref, _, _ = _result(tmp_path)
    with pytest.raises(StageBindingError, match="MODEL_INFERENCE_REQUIRES_WORKER_ROUTER_SELECTION"):
        _model_stage(store, ModelExplanationAdapter(FixedGenerator("prose")), [result_ref], router=None)


def test_explainers_only_consume_result_artifacts(tmp_path: Path) -> None:
    store, source, result_ref, record_ref, _ = _result(tmp_path)
    adapter = ModelExplanationAdapter(FixedGenerator("prose"))
    for wrong in (source, record_ref):
        with pytest.raises(CapabilityAdapterError, match="EXPLANATION_INPUT_NOT_RESULT"):
            _model_stage(store, adapter, [wrong])
        with pytest.raises(CapabilityAdapterError, match="EXPLANATION_INPUT_NOT_RESULT"):
            run_stage(store, TEMPLATE_CAPABILITY, TemplateExplanationAdapter(), [wrong])
    with pytest.raises(CapabilityAdapterError, match="EXPLANATION_PARAMETERS_NOT_ACCEPTED"):
        _model_stage(store, adapter, [result_ref], {"instruction": "rewrite the rules"})
    explanation = _model_stage(store, adapter, [result_ref]).outputs[0]
    with pytest.raises(CapabilityAdapterError, match="EXPLANATION_INPUT_NOT_RESULT"):
        _model_stage(store, adapter, [explanation])  # no explanation-of-explanation laundering


@pytest.mark.parametrize("text,code", [
    ("", "EXPLANATION_TEXT_INVALID"),
    ("x" * 4001, "EXPLANATION_TEXT_INVALID"),
    ("bell\x07", "EXPLANATION_TEXT_INVALID"),
    ("leaked " + "s" + "k-" + "y" * 12, "EXPLANATION_CREDENTIAL_SHAPED"),
])
def test_model_text_is_bounded_and_credential_free(tmp_path: Path, text: str, code: str) -> None:
    store, _, result_ref, _, _ = _result(tmp_path)
    with pytest.raises(CapabilityAdapterError) as caught:
        _model_stage(store, ModelExplanationAdapter(FixedGenerator(text)), [result_ref])
    assert caught.value.code == code


def test_template_text_is_bounded_deterministically() -> None:
    facts = {"algorithm_id": "drsa.domlem", "algorithm_version": "1", "result_sha256": "c" * 64, "metrics": {},
             "highlights": ["if " + " and ".join(f"c{i} >= {'v' * 60}" for i in range(32)) for _ in range(8)]}
    text = render_template(facts)
    assert len(text) == MAX_TEXT and text.endswith("[truncated]") and text == render_template(facts)
