# SPDX-License-Identifier: AGPL-3.0-only
"""Explanations of algorithm results: prose labelled EXPLANATION, never a result, never authority.

An LLM must not pretend to be the algorithm. The ``RESULT_EXPLANATION`` capability (MODEL_RUNTIME; the
adapter delegates model choice to the existing Worker Router, so a stage needs router selection evidence)
may consume ONLY a validated ``algorithms.result`` artifact. The model receives a bounded set of facts
derived from that artifact, never the dataset, and its text is written as a separate
``algorithms.explanation`` artifact that names the source result hash. It cannot alter the result:
artifacts are content-addressed and the adapter gets no write path except a new explanation artifact, and
``load_result`` refuses anything that is not a result. ``TemplateExplanationAdapter`` is the deterministic
stand-in for tests and offline use.

Before any fact is extracted the source must name a REGISTERED, executable algorithm with its current
version and capability, and its ``result`` body must validate against that algorithm's own result
schema; facts are read only from the validated model. The whole facts payload is then bounded (canonical
bytes, items, string length) and passed through ``assert_minimized`` (credential-shaped values or names
are refused) BEFORE any generator is called. Both adapters are closed-error boundaries: failures are
closed codes raised outside the handler, so no exception chain carries result content.
"""
from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from typing import Any, Protocol

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    Modality,
    VerificationMethod,
)
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.capability.artifacts import ArtifactAccessDenied
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.packs.algorithms import drsa, fcm, mcdm
from pados.packs.algorithms.contracts import (
    JSON_MEDIA,
    MAX_RESULT_BYTES,
    RESULT_KIND,
    AlgorithmError,
    ResultEnvelope,
    canonical_digest,
    canonical_json,
    load_result,
    no_credentials,
)
from pados.packs.algorithms.registry import ALGORITHMS

EXPLANATION_SCHEMA = "pados.algorithms.explanation.v1"
EXPLANATION_KIND = "algorithms.explanation"
EXPLANATION_LABEL = "EXPLANATION"
MAX_TEXT = 4000
MAX_FACT_ITEMS = 8
MAX_FACTS_BYTES = 16 * 1024  # canonical JSON bytes of the whole facts payload
MAX_FACT_STRING = 2048
MAX_FACT_COLLECTION = 64
FACT_KEYS = frozenset({"algorithm_id", "algorithm_version", "capability_id", "result_sha256", "metrics",
                       "highlights"})
_TOO_LARGE = frozenset({"RECORD_TOO_LARGE", "COLLECTION_TOO_LARGE", "STRING_TOO_LONG", "RECORD_TOO_DEEP"})
_TEXT_FORBIDDEN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def explanation_capability(network_requirement: NetworkRequirement = NetworkRequirement.LOCAL_ONLY
                           ) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id="RESULT_EXPLANATION", version="1", family=CapabilityFamily.MODEL_RUNTIME,
        operation="EXPLAIN_ALGORITHM_RESULT", input_contract="algorithms.result/v1",
        output_contract="algorithms.explanation/v1", determinism=Determinism.PROBABILISTIC,
        modalities=(Modality.STRUCTURED, Modality.TEXT), max_classification=DataClassification.RESTRICTED,
        network_requirement=network_requirement, verification=(VerificationMethod.HUMAN_REVIEW,),
        description="Prose explanation of an algorithm result, never a replacement for it")


TEMPLATE_CAPABILITY = CapabilityDescriptor(
    capability_id="RESULT_EXPLANATION_TEMPLATE", version="1", family=CapabilityFamily.ALGORITHM,
    operation="EXPLAIN_ALGORITHM_RESULT", input_contract="algorithms.result/v1",
    output_contract="algorithms.explanation/v1", determinism=Determinism.DETERMINISTIC,
    modalities=(Modality.STRUCTURED, Modality.TEXT), max_classification=DataClassification.RESTRICTED,
    network_requirement=NetworkRequirement.LOCAL_ONLY,
    verification=(VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY),
    description="Deterministic template explanation of an algorithm result")


def _highlights(result: object) -> list[str]:
    """Highlights read only from a schema-validated result model."""
    if isinstance(result, drsa.DrsaResult):
        return ["if " + " and ".join(f"{c.attribute} {c.operator} {c.value}" for c in rule.conditions)
                + f" then {rule.conclusion.union} {rule.conclusion.decision_class} (support {rule.support})"
                for rule in result.rules[:MAX_FACT_ITEMS]]
    if isinstance(result, fcm.FcmResult):
        return [f"status {result.status.value} after {result.iterations} iterations"] + [
            f"{name} = {value}" for name, value in list(result.final_state.items())[:MAX_FACT_ITEMS]]
    if isinstance(result, mcdm.DematelResult):
        return [f"{s.factor}: {s.group} (prominence {s.prominence}, relation {s.relation})"
                for s in result.scores[:MAX_FACT_ITEMS]]
    raise AlgorithmError("EXPLANATION_ALGORITHM_UNKNOWN")


def _bounded_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Size bounds and the minimization guard on the exact payload a generator would receive."""
    if len(canonical_json(facts)) > MAX_FACTS_BYTES:
        raise AlgorithmError("EXPLANATION_FACTS_TOO_LARGE")
    code: str | None = None
    try:
        assert_minimized(facts, allowed_keys=FACT_KEYS, max_string=MAX_FACT_STRING,
                         max_items=MAX_FACT_COLLECTION, max_total_bytes=MAX_FACTS_BYTES)
    except DataMinimizationViolation as exc:
        if exc.code.startswith("CREDENTIAL_SHAPED"):
            code = "EXPLANATION_FACTS_CREDENTIAL_SHAPED"
        elif exc.code in _TOO_LARGE:
            code = "EXPLANATION_FACTS_TOO_LARGE"
        else:
            code = "EXPLANATION_FACTS_NOT_MINIMIZED"
    if code is not None:
        raise AlgorithmError(code)
    return facts


def result_facts(envelope: ResultEnvelope, result_sha256: str) -> dict[str, Any]:
    """Bounded, minimized facts taken only from a validated result of a registered algorithm."""
    spec = ALGORITHMS.get(envelope.algorithm_id)
    if spec is None or spec.compute is None:
        raise AlgorithmError("EXPLANATION_ALGORITHM_UNKNOWN")
    if envelope.algorithm_version != spec.algorithm_version:
        raise AlgorithmError("EXPLANATION_ALGORITHM_VERSION_MISMATCH")
    if envelope.capability_id != spec.capability.capability_id:
        raise AlgorithmError("EXPLANATION_CAPABILITY_MISMATCH")
    result = spec.parse_result(envelope.result, "EXPLANATION_RESULT_SCHEMA_INVALID")
    facts: dict[str, Any] = {
        "algorithm_id": envelope.algorithm_id, "algorithm_version": envelope.algorithm_version,
        "capability_id": envelope.capability_id, "result_sha256": result_sha256,
        "metrics": dict(sorted(envelope.metrics.items())), "highlights": _highlights(result),
    }
    return _bounded_facts(facts)


def render_template(facts: Mapping[str, Any]) -> str:
    lines = [f"Explanation (not a result) of {facts['algorithm_id']} v{facts['algorithm_version']}"
             f" result {facts['result_sha256'][:12]}."]
    lines += [f"- {name}: {value}" for name, value in facts["metrics"].items()]
    lines += [f"- {item}" for item in facts["highlights"]]
    text = "\n".join(lines)
    marker = "\n[truncated]"
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - len(marker)] + marker


def _checked_text(text: object) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or _TEXT_FORBIDDEN.search(text):
        raise AlgorithmError("EXPLANATION_TEXT_INVALID")
    no_credentials({"text": text}, "EXPLANATION_CREDENTIAL_SHAPED")
    return text


def _closed(exc: Exception) -> str:
    return exc.code if isinstance(exc, AlgorithmError) else "EXPLANATION_UNEXPECTED_ERROR"


def _source(request: StageRequest, context: StageContext) -> tuple[ResultEnvelope, str]:
    if request.seed is not None:
        raise AlgorithmError("EXPLANATION_SEED_NOT_APPLICABLE")
    if len(request.inputs) != 1:
        raise AlgorithmError("EXPLANATION_INPUT_COUNT_INVALID")
    ref = request.inputs[0]
    if ref.kind != RESULT_KIND or ref.media_type != JSON_MEDIA or ref.size_bytes > MAX_RESULT_BYTES:
        raise AlgorithmError("EXPLANATION_INPUT_NOT_RESULT")
    if request.parameters:
        raise AlgorithmError("EXPLANATION_PARAMETERS_NOT_ACCEPTED")
    return load_result(context.artifacts.get(ref), ref), ref.sha256


def _write(context: StageContext, facts: Mapping[str, Any], text: str, *, explainer: str, adapter_id: str,
           router_selection_hash: str | None) -> StageOutput:
    document = {
        "schema": EXPLANATION_SCHEMA, "label": EXPLANATION_LABEL, "authoritative": False,
        "semantic_authority": "NONE", "source_result_sha256": facts["result_sha256"],
        "source_algorithm_id": facts["algorithm_id"], "explainer": explainer, "explainer_adapter_id": adapter_id,
        "router_selection_hash": router_selection_hash, "facts_hash": canonical_digest(dict(facts)), "text": text,
    }
    ref = context.artifacts.put(canonical_json(document), media_type=JSON_MEDIA, kind=EXPLANATION_KIND)
    return StageOutput(outputs=(ref,), metrics={"text_chars": float(len(text))},
                       evidence={"explanation_sha256": ref.sha256, "source_result_sha256": facts["result_sha256"],
                                 "explainer": explainer, "label": EXPLANATION_LABEL})


class TemplateExplanationAdapter:
    """Deterministic, model-free explainer (the stand-in for a model explanation)."""

    def __init__(self) -> None:
        self._descriptor = AdapterDescriptor(
            adapter_id="algorithms.explain.template", capability_id=TEMPLATE_CAPABILITY.capability_id, version="1",
            backend="python", determinism=Determinism.DETERMINISTIC,
            max_classification=DataClassification.RESTRICTED, network_requirement=NetworkRequirement.LOCAL_ONLY)

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return True

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        try:
            envelope, sha = _source(request, context)
            facts = result_facts(envelope, sha)
            text = _checked_text(render_template(facts))
            return _write(context, facts, text, explainer="TEMPLATE", adapter_id=self._descriptor.adapter_id,
                          router_selection_hash=None)
        except ArtifactAccessDenied:
            raise
        except Exception as exc:  # noqa: BLE001 - closed-error boundary
            code = _closed(exc)
        raise CapabilityAdapterError(code) from None  # outside the handler: no exception chain


class ExplanationGenerator(Protocol):
    """Model text generation for the worker the Worker Router selected (wired by the fabric, not here)."""

    def generate(self, facts: Mapping[str, Any], *, router_selection_hash: str) -> str: ...


class ModelExplanationAdapter:
    """Model-backed explainer: selection stays with the Worker Router; output is labelled prose only."""

    def __init__(self, generator: ExplanationGenerator | None = None, *,
                 max_classification: DataClassification = DataClassification.INTERNAL,
                 network_requirement: NetworkRequirement = NetworkRequirement.LOCAL_ONLY) -> None:
        self._generator = generator
        self._descriptor = AdapterDescriptor(
            adapter_id="algorithms.explain.model", capability_id="RESULT_EXPLANATION", version="1",
            backend="worker-router", determinism=Determinism.PROBABILISTIC, max_classification=max_classification,
            network_requirement=network_requirement, delegates_to_worker_router=True)

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return self._generator is not None

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        try:
            if self._generator is None:
                raise AlgorithmError("EXPLANATION_GENERATOR_UNAVAILABLE")
            selection = request.router_selection_hash
            if not isinstance(selection, str) or not re.fullmatch(r"[0-9a-f]{64}", selection):
                raise AlgorithmError("EXPLANATION_REQUIRES_WORKER_ROUTER_SELECTION")
            envelope, sha = _source(request, context)
            facts = result_facts(envelope, sha)  # validated, bounded and minimized before the generator runs
            text = _checked_text(self._generate(facts, selection))
            return _write(context, facts, text, explainer="MODEL", adapter_id=self._descriptor.adapter_id,
                          router_selection_hash=selection)
        except ArtifactAccessDenied:
            raise
        except Exception as exc:  # noqa: BLE001 - closed-error boundary
            code = _closed(exc)
        raise CapabilityAdapterError(code) from None  # outside the handler: no exception chain

    def _generate(self, facts: Mapping[str, Any], selection: str) -> object:
        generator = self._generator
        if generator is None:
            raise AlgorithmError("EXPLANATION_GENERATOR_UNAVAILABLE")
        try:
            # The generator gets a private deep copy: it cannot mutate the facts that are hashed and recorded.
            return generator.generate(copy.deepcopy(dict(facts)), router_selection_hash=selection)
        except Exception:  # noqa: BLE001 - a generator failure is a closed code, never its message
            pass
        raise AlgorithmError("EXPLANATION_GENERATOR_FAILED")
