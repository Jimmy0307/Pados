"""Explanation sources are validated, bounded and minimized BEFORE any generator runs (CR-14 M-03).

A forged ``algorithms.result`` artifact (any bytes stored under that kind) must not reach a model: the
envelope must name a registered algorithm at its current version and capability, the body must satisfy
that algorithm's own result schema, and the facts derived from it are bounded and credential-free.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from _algo_harness import exception_chain, put_json, run_stage, students
from pados.capability.adapter import CapabilityAdapterError
from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts
from pados.enterprise.classification import DataClassification
from pados.packs.algorithms import AlgorithmAdapter
from pados.packs.algorithms.contracts import JSON_MEDIA, RESULT_KIND, canonical_json
from pados.packs.algorithms.explanation import (
    TEMPLATE_CAPABILITY,
    ModelExplanationAdapter,
    TemplateExplanationAdapter,
    explanation_capability,
)
from pados.packs.algorithms.registry import DRSA

ROUTER_HASH = "a" * 64
MARKER = "zzmarker"


class RecordingGenerator:
    """Test double: records every call; the refusals below must leave ``calls`` empty."""

    def __init__(self, failure: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failure = failure

    def generate(self, facts: Mapping[str, Any], *, router_selection_hash: str) -> str:
        self.calls.append(dict(facts))
        if self.failure is not None:
            raise self.failure
        return "prose about the result"


def _envelope(tmp_path: Path) -> tuple[ObjectStoreArtifacts, dict[str, Any]]:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    output = run_stage(store, DRSA.capability, AlgorithmAdapter(DRSA), [put_json(store, students())], {})
    return store, json.loads(store.get(output.outputs[0]))


def _forged(store: ObjectStoreArtifacts, envelope: dict[str, Any]) -> ArtifactRef:
    """Canonical bytes stored under the result kind: ``load_result`` accepts the envelope shape."""
    return store.put(canonical_json(envelope), media_type=JSON_MEDIA, kind=RESULT_KIND)


def _long_rules(envelope: dict[str, Any]) -> None:
    rule = envelope["result"]["rules"][0]
    rule["conditions"] = [{"attribute": f"a{i}" + "x" * 60, "operator": ">=", "value": "v" * 64} for i in range(31)]
    envelope["result"]["rules"] = [rule] * 8


def _credential_value(envelope: dict[str, Any]) -> None:
    envelope["result"]["rules"][0]["conditions"][0]["value"] = "s" + "k-" + MARKER + "x" * 12  # runtime-assembled


def _credential_metric(envelope: dict[str, Any]) -> None:
    envelope["metrics"]["to" + "ken"] = 1.0  # a prohibited field name reaching the facts


FORGERIES: list[tuple[str, Callable[[dict[str, Any]], None], str]] = [
    ("unknown_algorithm", lambda e: e.update(algorithm_id="zzmarker.algo"), "EXPLANATION_ALGORITHM_UNKNOWN"),
    ("contract_only_algorithm", lambda e: e.update(algorithm_id="mcdm.danp", algorithm_version="0"),
     "EXPLANATION_ALGORITHM_UNKNOWN"),
    ("stale_version", lambda e: e.update(algorithm_version="1"), "EXPLANATION_ALGORITHM_VERSION_MISMATCH"),
    ("other_capability", lambda e: e.update(capability_id="FUZZY_COGNITIVE_MAP"), "EXPLANATION_CAPABILITY_MISMATCH"),
    ("malformed_rules", lambda e: e["result"].update(rules=MARKER), "EXPLANATION_RESULT_SCHEMA_INVALID"),
    ("missing_rules", lambda e: e["result"].pop("rules"), "EXPLANATION_RESULT_SCHEMA_INVALID"),
    ("extra_field", lambda e: e["result"].update(zzmarker=1), "EXPLANATION_RESULT_SCHEMA_INVALID"),
    ("fcm_body_under_drsa_id", lambda e: e.update(result={"concepts": ["A"], "status": "FIXED_POINT",
                                                          "iterations": 1, "final_state": {"A": 0.5}}),
     "EXPLANATION_RESULT_SCHEMA_INVALID"),
    ("unbounded_version", lambda e: e.update(algorithm_version="2" * 500), "RESULT_ALGORITHM_VERSION_INVALID"),
    ("unbounded_capability", lambda e: e.update(capability_id="R" * 500), "RESULT_CAPABILITY_ID_INVALID"),
    ("oversized_facts", _long_rules, "EXPLANATION_FACTS_TOO_LARGE"),
    ("credential_shaped_value", _credential_value, "EXPLANATION_FACTS_CREDENTIAL_SHAPED"),
    ("prohibited_metric_name", _credential_metric, "EXPLANATION_FACTS_NOT_MINIMIZED"),
]


@pytest.mark.parametrize("name,mutate,code", FORGERIES, ids=[f[0] for f in FORGERIES])
def test_forged_sources_are_refused_before_the_generator(tmp_path: Path, name: str,
                                                         mutate: Callable[[dict[str, Any]], None], code: str) -> None:
    store, envelope = _envelope(tmp_path)
    mutate(envelope)
    ref = _forged(store, envelope)
    generator = RecordingGenerator()
    with pytest.raises(CapabilityAdapterError) as caught:
        run_stage(store, explanation_capability(), ModelExplanationAdapter(generator), [ref],
                  classification=DataClassification.INTERNAL, router_selection_hash=ROUTER_HASH)
    assert caught.value.code == code
    assert generator.calls == []  # the model never saw the forged source
    assert exception_chain(caught.value) == [caught.value] and MARKER not in repr(caught.value)
    with pytest.raises(CapabilityAdapterError) as template:  # the deterministic explainer refuses identically
        run_stage(store, TEMPLATE_CAPABILITY, TemplateExplanationAdapter(), [ref])
    assert template.value.code == code


def test_valid_source_reaches_the_generator_with_bounded_facts(tmp_path: Path) -> None:
    store, envelope = _envelope(tmp_path)
    ref = _forged(store, envelope)  # identical canonical bytes: this IS the real result
    generator = RecordingGenerator()
    output = run_stage(store, explanation_capability(), ModelExplanationAdapter(generator), [ref],
                       classification=DataClassification.INTERNAL, router_selection_hash=ROUTER_HASH)
    assert len(generator.calls) == 1 and output.outputs[0].kind == "algorithms.explanation"
    assert len(canonical_json(generator.calls[0])) <= 16 * 1024


def test_generator_failures_are_closed_and_echo_nothing(tmp_path: Path) -> None:
    store, envelope = _envelope(tmp_path)
    ref = _forged(store, envelope)
    generator = RecordingGenerator(RuntimeError(f"provider said {MARKER}"))
    with pytest.raises(CapabilityAdapterError) as caught:
        run_stage(store, explanation_capability(), ModelExplanationAdapter(generator), [ref],
                  classification=DataClassification.INTERNAL, router_selection_hash=ROUTER_HASH)
    assert caught.value.code == "EXPLANATION_GENERATOR_FAILED" and len(generator.calls) == 1
    assert exception_chain(caught.value) == [caught.value] and MARKER not in repr(caught.value)
