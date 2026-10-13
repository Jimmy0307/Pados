# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast

from pados.graph.schemas import CognitiveOperation, NodeType

_CANDIDATE_TYPES = {
    NodeType.CONCEPT.value,
    NodeType.ACTION.value,
    NodeType.RESOURCE.value,
    NodeType.OUTPUT.value,
}


class CognitiveProvider(Protocol):
    async def synthesize(self, specification: Mapping[str, Any]) -> str:
        """Generate from a bounded cognitive specification, not graph state."""


class CognitiveResponseError(ValueError):
    """Raised when a provider response cannot satisfy the V1 contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_MAX_ANSWER_CHARS = 20_000
_MAX_SUBQUESTIONS = 8
_MAX_GAPS = 8
_MAX_CLAIMS = 12
_MAX_PROPOSED_CHANGES = 3
_MAX_LIMITATIONS = 8
_MAX_EVIDENCE_REFS = 12


def _string(max_length: int, *, min_length: int = 0) -> dict[str, Any]:
    return {"type": "string", "minLength": min_length, "maxLength": max_length}


def _array(items: Mapping[str, Any], max_items: int) -> dict[str, Any]:
    return {"type": "array", "items": dict(items), "maxItems": max_items}


def _object(properties: Mapping[str, Any], required: Sequence[str]) -> dict[str, Any]:
    return {"type": "object", "properties": dict(properties), "required": list(required),
            "additionalProperties": False}


# The ONE cognitive response schema (same bounds as ``parse_cognitive_response``, which stays the validator).
COGNITIVE_RESPONSE_SCHEMA: dict[str, Any] = _object(
    {
        "title": _string(240),
        "answer": _string(_MAX_ANSWER_CHARS, min_length=1),
        "subquestions": _array(_object({"id": _string(120), "question": _string(500, min_length=1)},
                                       ["question"]), _MAX_SUBQUESTIONS),
        "gaps": _array(_object({"code": _string(80, min_length=1), "message": _string(500, min_length=1)},
                               ["code", "message"]), _MAX_GAPS),
        "claims": _array(_object({"claim": _string(1_000, min_length=1),
                                  "evidence_refs": _array(_string(160, min_length=1), _MAX_EVIDENCE_REFS)},
                                 ["claim"]), _MAX_CLAIMS),
        "evidence_refs": _array(_string(160, min_length=1), _MAX_EVIDENCE_REFS),
        "proposed_graph_changes": _array({"type": "object"}, _MAX_PROPOSED_CHANGES),
        "limitations": _array(_string(500, min_length=1), _MAX_LIMITATIONS),
    },
    ["answer"],
)


class ChatCognitiveProvider:
    """Adapt the existing chat provider to the structured cognitive contract."""

    def __init__(self, provider: Any, operation: CognitiveOperation) -> None:
        self.provider = provider
        self.operation = operation

    async def synthesize(self, specification: Mapping[str, Any]) -> str:
        prompt = build_prompt(self.operation, {"synthesis_spec": specification})
        return cast(str, await self.provider.chat(prompt, response_schema=COGNITIVE_RESPONSE_SCHEMA))


def build_prompt(operation: CognitiveOperation, context: Mapping[str, Any]) -> str:
    assembly = context.get("context_assembly")
    if not isinstance(assembly, Mapping):
        assembly = context
    provided_spec = context.get("synthesis_spec")
    spec = dict(provided_spec) if isinstance(provided_spec, Mapping) else {}
    if not spec:
        spec = {
            "goal": {
                "current_objective": " ".join(str(item) for item in assembly.get("root_context", [])),
            },
            "intent": str(assembly.get("action") or operation.value),
            "scope": {},
            "subquestions": [],
            "evidence": list(assembly.get("evidence", []))[:12],
            "references": list(assembly.get("resource_references", []))[:8],
            "gaps": list(assembly.get("gaps", []))[:8],
            "prior_synthesis": assembly.get("previous_synthesis"),
            "expected_structure": ["目前理解", "關鍵面向", "證據與缺口", "下一步"],
            "context_fingerprint": assembly.get("context_fingerprint", ""),
        }
    support = assembly.get("nodes", context.get("nodes", []))
    support = support if isinstance(support, Sequence) and not isinstance(support, (str, bytes, bytearray)) else []
    if support:
        spec["supporting_context"] = [
            {
                "id": str(node.get("id") or ""),
                "semantic_type": str(node.get("semantic_type") or ""),
                "context_role": str(node.get("context_role") or "CONFIRMED_CONCEPT"),
                "title": str(node.get("title") or "")[:240],
                "content": (
                    ""
                    if str(node.get("context_role") or "") in {"REFERENCE_ONLY", "SELECTED_EVIDENCE"}
                    or str(node.get("semantic_type") or "") == NodeType.RESOURCE.value
                    else str(node.get("content") or "")[:1_200]
                ),
            }
            for node in list(support)[:40]
            if isinstance(node, Mapping)
        ]
    else:
        prior_support = spec.get("supporting_context")
        spec["supporting_context"] = list(prior_support)[:40] if isinstance(prior_support, Sequence) and not isinstance(prior_support, (str, bytes, bytearray)) else []
    spec["context_fingerprint"] = str(spec.get("context_fingerprint") or assembly.get("context_fingerprint") or "")
    return (
        "You are a bounded cognitive assistant inside PADOS. Do not claim validation, "
        "take external action, or modify authoritative capability definitions. "
        f"Operation: {operation.value}. Cognitive specification JSON:\n"
        f"{json.dumps(spec, ensure_ascii=False)}\nEnd cognitive specification. "
        "Return only one JSON object with this bounded schema: "
        "{title:string, answer:string, subquestions:array, gaps:array, claims:array, "
        "evidence_refs:array, proposed_graph_changes:array, limitations:array}. "
        "answer is required. Each claim may contain claim:string and evidence_refs:array. "
        "Do not invent evidence IDs. proposed_graph_changes are proposals only and are not "
        "persisted automatically. All output is provisional for human inspection."
    )


def _json_array(value: str) -> list[Any] | None:
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else None
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", value)
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, list) else None


def parse_candidates(value: str, operation: CognitiveOperation) -> list[dict[str, str]]:
    """Legacy P6.16 adapter retained for compatibility tests only.

    The P6.17 cognitive service never calls this permissive candidate parser;
    provider execution uses :func:`parse_cognitive_response` below.
    """

    parsed = _json_array(value)
    candidates: list[dict[str, str]] = []
    for item in parsed or []:
        if not isinstance(item, Mapping):
            continue
        semantic_type = str(item.get("type", NodeType.CONCEPT.value)).upper()
        if semantic_type not in _CANDIDATE_TYPES:
            semantic_type = NodeType.CONCEPT.value
        title = str(item.get("title", "")).strip()[:240]
        content = str(item.get("content", "")).strip()[:20_000]
        if title:
            candidates.append({"type": semantic_type, "title": title, "content": content})
        if len(candidates) == 3:
            break
    if candidates:
        return candidates
    fallback = value.strip()[:20_000]
    if not fallback:
        fallback = "Provider returned no candidate content."
    return [
        {
            "type": NodeType.OUTPUT.value,
            "title": f"{operation.value.title()} candidate",
            "content": fallback,
        }
    ]


def _bounded_string(value: Any, *, field: str, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise CognitiveResponseError("INVALID_SCHEMA", f"{field} must be a string")
    result = value.strip()
    if required and not result:
        raise CognitiveResponseError("INVALID_SCHEMA", f"{field} is required")
    if len(result) > maximum:
        raise CognitiveResponseError("INVALID_SCHEMA", f"{field} exceeds its bounded length")
    return result


def parse_cognitive_response(value: str) -> dict[str, Any]:
    """Validate and normalize the strict provider response object."""

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise CognitiveResponseError("INVALID_RESPONSE", "provider response is not valid JSON") from exc
    if not isinstance(parsed, Mapping):
        raise CognitiveResponseError("INVALID_SCHEMA", "provider response must be a JSON object")
    allowed_fields = {
        "title",
        "answer",
        "subquestions",
        "gaps",
        "claims",
        "evidence_refs",
        "proposed_graph_changes",
        "limitations",
    }
    unknown_fields = sorted(set(parsed) - allowed_fields)
    if unknown_fields:
        raise CognitiveResponseError(
            "INVALID_SCHEMA",
            f"provider response contains unknown fields: {', '.join(str(field) for field in unknown_fields)}",
        )

    title = _bounded_string(parsed.get("title", ""), field="title", maximum=240)
    answer = _bounded_string(parsed.get("answer", ""), field="answer", maximum=_MAX_ANSWER_CHARS, required=True)

    def bounded_string_list(field: str, maximum_items: int, maximum_chars: int) -> list[str]:
        raw = parsed.get(field, [])
        if not isinstance(raw, list) or len(raw) > maximum_items:
            raise CognitiveResponseError("INVALID_SCHEMA", f"{field} must be a bounded array")
        return [_bounded_string(item, field=field, maximum=maximum_chars, required=True) for item in raw]

    raw_subquestions = parsed.get("subquestions", [])
    if not isinstance(raw_subquestions, list) or len(raw_subquestions) > _MAX_SUBQUESTIONS:
        raise CognitiveResponseError("INVALID_SCHEMA", "subquestions must be a bounded array")
    subquestions: list[dict[str, Any]] = []
    for item in raw_subquestions:
        if not isinstance(item, Mapping):
            raise CognitiveResponseError("INVALID_SCHEMA", "each subquestion must be an object")
        subquestions.append(
            {
                "id": _bounded_string(item.get("id", ""), field="subquestion.id", maximum=120),
                "question": _bounded_string(item.get("question", ""), field="subquestion.question", maximum=500, required=True),
            }
        )

    raw_gaps = parsed.get("gaps", [])
    if not isinstance(raw_gaps, list) or len(raw_gaps) > _MAX_GAPS:
        raise CognitiveResponseError("INVALID_SCHEMA", "gaps must be a bounded array")
    gaps: list[dict[str, str]] = []
    for item in raw_gaps:
        if not isinstance(item, Mapping):
            raise CognitiveResponseError("INVALID_SCHEMA", "each gap must be an object")
        gaps.append(
            {
                "code": _bounded_string(item.get("code", ""), field="gap.code", maximum=80, required=True),
                "message": _bounded_string(item.get("message", ""), field="gap.message", maximum=500, required=True),
            }
        )

    raw_claims = parsed.get("claims", [])
    if not isinstance(raw_claims, list) or len(raw_claims) > _MAX_CLAIMS:
        raise CognitiveResponseError("INVALID_SCHEMA", "claims must be a bounded array")
    claims: list[dict[str, Any]] = []
    for item in raw_claims:
        if not isinstance(item, Mapping):
            raise CognitiveResponseError("INVALID_SCHEMA", "each claim must be an object")
        refs = item.get("evidence_refs", [])
        if not isinstance(refs, list) or len(refs) > 12:
            raise CognitiveResponseError("INVALID_SCHEMA", "claim evidence_refs must be a bounded array")
        claims.append(
            {
                "claim": _bounded_string(item.get("claim", ""), field="claim.claim", maximum=1_000, required=True),
                "evidence_refs": [
                    _bounded_string(ref, field="claim.evidence_ref", maximum=160, required=True)
                    for ref in refs
                ],
            }
        )

    raw_changes = parsed.get("proposed_graph_changes", [])
    if not isinstance(raw_changes, list) or len(raw_changes) > _MAX_PROPOSED_CHANGES:
        raise CognitiveResponseError("INVALID_SCHEMA", "proposed_graph_changes must be a bounded array")
    proposed_changes = [dict(item) for item in raw_changes if isinstance(item, Mapping)]
    if len(proposed_changes) != len(raw_changes):
        raise CognitiveResponseError("INVALID_SCHEMA", "each proposed graph change must be an object")

    return {
        "title": title,
        "answer": answer,
        "subquestions": subquestions,
        "gaps": gaps,
        "claims": claims,
        "evidence_refs": bounded_string_list("evidence_refs", 12, 160),
        "proposed_graph_changes": proposed_changes,
        "limitations": bounded_string_list("limitations", _MAX_LIMITATIONS, 500),
    }


def validate_evidence_refs(response: Mapping[str, Any], allowed_ids: Sequence[str]) -> None:
    """Reject model-created evidence references that are absent from context."""

    allowed = {str(value) for value in allowed_ids}
    references = [str(value) for value in response.get("evidence_refs", [])]
    for claim in response.get("claims", []):
        if isinstance(claim, Mapping):
            references.extend(str(value) for value in claim.get("evidence_refs", []))
    unknown = sorted({value for value in references if value not in allowed})
    if unknown:
        raise CognitiveResponseError(
            "INVALID_SCHEMA",
            f"provider referenced evidence that is absent from context: {', '.join(unknown[:3])}",
        )


def candidate_position(nodes: Sequence[Mapping[str, Any]], index: int) -> tuple[float, float]:
    if not nodes:
        return 120.0 + index * 40.0, 120.0 + index * 40.0
    x = sum(float(node.get("x", 0)) for node in nodes) / len(nodes)
    y = sum(float(node.get("y", 0)) for node in nodes) / len(nodes)
    return x + 300.0, y + index * 150.0
