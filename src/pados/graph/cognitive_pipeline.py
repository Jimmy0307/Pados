# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded, provider-independent cognitive reasoning contracts.

The graph is a durable workspace, not a prompt.  This module turns a human
goal into a small, inspectable contract before a provider is called.  All
outputs are deliberately plain dictionaries so the contract can be persisted
in the existing JSON columns without a migration.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar
from uuid import uuid4

from pados.graph.evidence_contract import (
    evidence_record,
    evidence_state,
    source_record,
    validate_evidence,
)


class ScopeRelation(StrEnum):
    SAME_SCOPE_UPDATE = "SAME_SCOPE_UPDATE"
    SUBQUESTION = "SUBQUESTION"
    SCOPE_EXPANSION = "SCOPE_EXPANSION"
    NEW_OBJECTIVE = "NEW_OBJECTIVE"


class SynthesisMode(StrEnum):
    EXPLAIN = "EXPLAIN"
    EXPLORE = "EXPLORE"
    COMPARE = "COMPARE"
    CONNECT = "CONNECT"
    CRITIQUE = "CRITIQUE"
    PLAN = "PLAN"
    RESEARCH = "RESEARCH"
    SYNTHESIZE = "SYNTHESIZE"


_TOKEN_RE = re.compile(r"[0-9A-Za-z\u0080-\uffff]{2,}", re.UNICODE)
_STOPWORDS = {
    "about", "after", "again", "also", "because", "from", "have", "how", "into",
    "just", "like", "more", "that", "the", "this", "what", "when", "where", "which",
    "with", "would", "以及", "如果", "今天", "我要", "我想要", "整個", "這個", "需要",
}
_EXPANSION_MARKERS = re.compile(
    r"\b(ai|automation|automated|deploy|deployment|build|server|infrastructure|design|evaluate|operate|maintain)\b|"
    r"人工智慧|自動化|部署|建置|架設|伺服器|維運|維護|設計|評估|注意",
    re.IGNORECASE,
)
_QUESTION_MARKERS = re.compile(
    r"\?|？|\b(why|how|what|which|can|should|compare|difference)\b|為什麼|如何|怎麼|什麼|哪裡|是否|差異",
    re.IGNORECASE,
)
_DEPLOYMENT_MARKERS = re.compile(
    r"\b(ai|automation|automated|deploy|deployment|build|server|infrastructure|operate|maintain)\b|"
    r"人工智慧|自動化|部署|建置|架設|伺服器|基礎設施|維運|維護",
    re.IGNORECASE,
)


def _tokens(value: str) -> list[str]:
    result: list[str] = []
    for token in _TOKEN_RE.findall(value.casefold()):
        if token in _STOPWORDS or len(token) < 2:
            continue
        if token not in result:
            result.append(token)
    return result


def _normalise(value: str) -> str:
    return " ".join(value.strip().split())


def _first_topic(tokens: Sequence[str]) -> str:
    for token in tokens:
        if token.casefold() in {"ai", "automation"} and len(tokens) > 1:
            continue
        if re.search(r"[a-z]", token, re.IGNORECASE) and len(token) <= 40:
            return token.upper() if token.isupper() or len(token) <= 4 else token
    return tokens[0] if tokens else "目前問題"


def _topic_terms(text: str, prior: Mapping[str, Any] | None) -> list[str]:
    current = _tokens(text)
    previous = prior.get("topic_terms") if isinstance(prior, Mapping) else None
    previous_terms = [str(item) for item in previous] if isinstance(previous, Sequence) else []
    shared = [item for item in previous_terms if item in current]
    ordered = shared + [item for item in current if item not in shared]
    return ordered[:8] or previous_terms[:8] or current[:8]


def _shared_topic_terms(previous: Sequence[str], current: Sequence[str]) -> set[str]:
    """Allow stable stem-like topic continuity without a domain dictionary."""

    return {
        prior
        for prior in previous
        if any(
            prior == item
            or (len(prior) >= 3 and len(item) >= 3 and (prior.startswith(item) or item.startswith(prior)))
            for item in current
        )
    }


def _mode(text: str, route_class: str) -> SynthesisMode:
    lowered = text.casefold()
    if re.search(r"compare|versus|difference|比較|差異", lowered, re.IGNORECASE):
        return SynthesisMode.COMPARE
    if re.search(r"connect|relationship|relate|關係|連結|關聯", lowered, re.IGNORECASE):
        return SynthesisMode.CONNECT
    if re.search(r"critique|weakness|risk|conflict|review|批判|弱點|風險|衝突|檢視", lowered, re.IGNORECASE):
        return SynthesisMode.CRITIQUE
    if route_class == "RESEARCH" or re.search(r"research|source|article|paper|研究|資料|來源|文章|論文", lowered, re.IGNORECASE):
        return SynthesisMode.RESEARCH
    if route_class == "PLANNING" or _DEPLOYMENT_MARKERS.search(text):
        return SynthesisMode.PLAN
    if re.search(r"explore|expand|延伸|探索|可能", lowered, re.IGNORECASE):
        return SynthesisMode.EXPLORE
    if route_class in {"INFORMATIONAL", "COGNITIVE"}:
        return SynthesisMode.EXPLAIN
    return SynthesisMode.SYNTHESIZE


@dataclass(frozen=True)
class GoalInterpretation:
    topic: str
    topic_terms: tuple[str, ...]
    original_objective: str
    current_objective: str
    decision: str
    intent_class: str
    synthesis_mode: SynthesisMode
    scope_relation: ScopeRelation
    objective_changed: bool
    scope_id: str
    parent_scope_id: str | None
    scope_title: str
    latest_input_kind: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "topic_terms": list(self.topic_terms),
            "original_objective": self.original_objective,
            "current_objective": self.current_objective,
            "decision": self.decision,
            "intent_class": self.intent_class,
            "synthesis_mode": self.synthesis_mode.value,
            "scope_relation": self.scope_relation.value,
            "objective_changed": self.objective_changed,
            "scope_id": self.scope_id,
            "parent_scope_id": self.parent_scope_id,
            "scope_title": self.scope_title,
            "latest_input_kind": self.latest_input_kind,
        }


class CognitiveGoalInterpreter:
    """Interpret the latest goal against the latest known cognitive scope."""

    def interpret(
        self,
        text: str,
        *,
        intent_class: str = "COGNITIVE",
        prior_goals: Sequence[Mapping[str, Any]] = (),
    ) -> GoalInterpretation:
        current = _normalise(text)
        latest = next((item for item in prior_goals if isinstance(item, Mapping)), None)
        prior_current = _normalise(str(latest.get("current_objective") or latest.get("interpreted_goal") or "")) if latest else ""
        prior_terms = _topic_terms(prior_current, latest) if latest else []
        current_terms = _tokens(current)
        shared = _shared_topic_terms(prior_terms, current_terms)
        prior_scope_id = str(latest.get("scope_id") or "") if latest else ""
        parent_scope_id = prior_scope_id or None
        if not latest:
            relation = ScopeRelation.NEW_OBJECTIVE
            original = current
            scope_id = str(uuid4())
            input_kind = "new_objective"
        elif current.casefold() == prior_current.casefold():
            relation = ScopeRelation.SAME_SCOPE_UPDATE
            original = str(latest.get("original_objective") or prior_current)
            scope_id = prior_scope_id or str(uuid4())
            parent_scope_id = str(latest.get("parent_scope_id") or "") or None
            input_kind = "refinement"
        elif shared and _EXPANSION_MARKERS.search(current):
            relation = ScopeRelation.SCOPE_EXPANSION
            original = str(latest.get("original_objective") or prior_current)
            scope_id = str(uuid4())
            input_kind = "scope_expansion"
        elif shared:
            relation = ScopeRelation.SUBQUESTION
            original = str(latest.get("original_objective") or prior_current)
            scope_id = prior_scope_id or str(uuid4())
            input_kind = "subquestion"
        else:
            relation = ScopeRelation.NEW_OBJECTIVE
            original = current
            scope_id = str(uuid4())
            parent_scope_id = None
            input_kind = "new_objective"

        terms = _topic_terms(current, latest if shared else None)
        topic = _first_topic(terms)
        mode = _mode(current, intent_class)
        decision = (
            f"評估並規劃如何安全地處理：{current}"
            if mode == SynthesisMode.PLAN or relation == ScopeRelation.SCOPE_EXPANSION
            else f"釐清並回答：{current}"
        )
        safety_markers = re.search(r"sandbox|security|defen[cs]e|risk|安全|防範|隔離|風險", current, re.IGNORECASE)
        has_ai = bool(re.search(r"\bai\b|人工智慧", current, re.IGNORECASE))
        if safety_markers and has_ai:
            scope_title = f"AI 與 {topic} 安全隔離"
        elif relation == ScopeRelation.SCOPE_EXPANSION and _DEPLOYMENT_MARKERS.search(current):
            scope_title = f"{topic} 部署與維護"
        elif re.search(r"architecture|system|架構|系統", current, re.IGNORECASE):
            scope_title = f"{topic} 系統架構"
        else:
            scope_title = current[:100] or "目前認知問題"
        return GoalInterpretation(
            topic=topic,
            topic_terms=tuple(terms),
            original_objective=original,
            current_objective=current,
            decision=decision,
            intent_class=intent_class,
            synthesis_mode=mode,
            scope_relation=relation,
            objective_changed=relation in {ScopeRelation.SCOPE_EXPANSION, ScopeRelation.NEW_OBJECTIVE},
            scope_id=scope_id,
            parent_scope_id=parent_scope_id,
            scope_title=scope_title,
            latest_input_kind=input_kind,
        )


class ProblemDecomposer:
    """Create bounded subquestions from general goal signals."""

    _DEPLOYMENT_DIMENSIONS = (
        ("architecture", "架構與角色", "先界定系統邊界、元件與責任分工"),
        ("configuration", "設定與部署", "確認設定、環境與變更順序"),
        ("security", "安全與權限", "檢查權限、信任邊界與失敗風險"),
        ("ai_reliability", "AI 協作可靠性", "界定 AI 可協助與必須人工檢查的部分"),
        ("validation", "驗證與測試", "定義可觀察的檢查與驗證條件"),
        ("governance", "部署治理", "保留核准、審計與人工確認邊界"),
        ("monitoring", "監控與維運", "持續觀察狀態、異常與影響"),
        ("rollback", "回復與復原", "預先設計可逆變更與回復路徑"),
    )
    _GENERAL_DIMENSIONS = (
        ("core_concept", "核心概念", "先建立共同語彙與問題邊界"),
        ("key_dimensions", "關鍵面向", "拆出需要分別理解的部分"),
        ("evidence", "可用證據", "找出能支持結論的來源內容"),
        ("gaps", "未知與限制", "標示目前還不能可靠回答的部分"),
        ("next_step", "下一步", "安排最小且可回看的後續調查"),
    )

    def decompose(self, goal: GoalInterpretation) -> list[dict[str, str]]:
        text = goal.current_objective
        dimensions = self._DEPLOYMENT_DIMENSIONS if _DEPLOYMENT_MARKERS.search(text) else self._GENERAL_DIMENSIONS
        result: list[dict[str, str]] = []
        for key, title, rationale in dimensions:
            semantic_title = f"{goal.topic}：{title}"[:120]
            result.append(
                {
                    "id": key,
                    "title": semantic_title,
                    "question": f"針對「{goal.current_objective}」，{rationale}；需要回答：{semantic_title}。",
                    "rationale": rationale,
                    "kind": "SUBQUESTION",
                }
            )
        return result[:8]


class EvidenceResolver:
    """Resolve resource references into bounded, provenance-bearing evidence."""

    max_units = 12
    max_units_per_source = 3
    max_unit_chars = 1200

    def resolve(
        self,
        nodes: Sequence[Mapping[str, Any]],
        *,
        query: str,
    ) -> dict[str, Any]:
        query_terms = set(_tokens(query))
        references: list[dict[str, Any]] = []
        units: list[dict[str, Any]] = []
        gaps: list[dict[str, str]] = []
        resources = [node for node in nodes if str(node.get("semantic_type")) == "RESOURCE"]
        seen_evidence_keys: set[str] = set()
        for source in resources[:8]:
            metadata = source.get("metadata_json")
            metadata = metadata if isinstance(metadata, Mapping) else {}
            content = str(source.get("content") or "").strip()
            status = str(metadata.get("retrieval_status") or "UNKNOWN")
            evidence_eligibility = str(metadata.get("evidence_eligibility") or "UNKNOWN")
            available = bool(content) and status not in {"UNSUPPORTED", "FAILED", "BLOCKED"}
            eligible = available and evidence_eligibility == "EVIDENCE_ELIGIBLE"
            reference = source_record(source)
            reference.update(
                {
                    "kind": "REFERENCE",
                    "context_role": "REFERENCE_ONLY",
                    "source_node_id": reference["source_id"],
                    "url": reference["origin"],
                    "content_available": available,
                }
            )
            references.append(reference)
            if not available:
                gaps.append(
                    {
                        "code": "SOURCE_CONTENT_UNAVAILABLE",
                        "message": "已加入來源，但尚未取得可供分析的內容。",
                        "source_node_id": reference["source_id"],
                    }
                )
                continue
            if not eligible:
                gaps.append(
                    {
                        "code": "EVIDENCE_NOT_ELIGIBLE",
                        "message": "已取得內容，但尚未確認是否適合作為證據。",
                        "source_node_id": reference["source_id"],
                    }
                )
                continue
            paragraphs = [part.strip() for part in re.split(r"\n\s*\n|(?<=[。！？.!?])\s+", content) if part.strip()]
            candidates: list[tuple[int, int, str]] = []
            for index, paragraph in enumerate(paragraphs):
                for start in range(0, max(len(paragraph), 1), self.max_unit_chars):
                    excerpt = paragraph[start : start + self.max_unit_chars].strip()
                    if not excerpt:
                        continue
                    score = sum(1 for term in query_terms if term in excerpt.casefold())
                    if score:
                        candidates.append((score, index, excerpt))
            candidates.sort(key=lambda item: (-item[0], item[1]))
            source_key = str(metadata.get("extracted_text_hash") or metadata.get("content_hash") or source.get("id") or "")
            if source_key and source_key in seen_evidence_keys:
                continue
            for unit_index, (score, paragraph_index, excerpt) in enumerate(candidates[: self.max_units_per_source]):
                item = evidence_record(
                    source=source,
                    evidence_id=f"{source.get('id')}:{paragraph_index}:{unit_index}",
                    excerpt=excerpt,
                    location={"paragraph_index": paragraph_index},
                    relevance_score=score,
                )
                item["source_title"] = str(source.get("title") or "Untitled source")
                units.append(item)
                if len(units) >= self.max_units:
                    break
            if candidates and source_key:
                seen_evidence_keys.add(source_key)
            if len(units) >= self.max_units:
                break
            if not candidates:
                gaps.append(
                    {
                        "code": "EVIDENCE_NOT_SELECTED",
                        "message": "已取得內容，但尚未取得可供分析的證據。",
                        "source_node_id": reference["source_id"],
                    }
                )
        units, rejected = validate_evidence(references, units)
        if rejected:
            gaps.append(
                {
                    "code": "EVIDENCE_PROVENANCE_INVALID",
                    "message": "部分證據缺少可追溯的來源、版本或文字範圍，因此未納入認知脈絡。",
                }
            )
        return {
            "references": references,
            "evidence": units[: self.max_units],
            "evidence_state": evidence_state(references, units),
            "gaps": gaps[:8],
        }


class GapDetector:
    """Report missing information relative to the actual objective."""

    def detect(
        self,
        goal: GoalInterpretation,
        decomposition: Sequence[Mapping[str, Any]],
        evidence: Mapping[str, Any],
    ) -> list[dict[str, str]]:
        gaps = [dict(item) for item in evidence.get("gaps", []) if isinstance(item, Mapping)]
        text = goal.current_objective
        if goal.synthesis_mode == SynthesisMode.PLAN or goal.scope_relation == ScopeRelation.SCOPE_EXPANSION:
            if not re.search(r"\b(os|linux|windows|mac|container|cloud|kubernetes)\b|作業系統|環境", text, re.IGNORECASE):
                gaps.append({"code": "DEPLOYMENT_ENVIRONMENT_UNKNOWN", "message": "尚未知道實際執行環境與部署邊界。"})
            if not re.search(r"\b(public|private|internal|network|topology)\b|公開|內部|網路|拓撲", text, re.IGNORECASE):
                gaps.append({"code": "DEPLOYMENT_BOUNDARY_UNKNOWN", "message": "尚未知道網路邊界、責任歸屬與服務範圍。"})
        if not evidence.get("evidence"):
            if evidence.get("references"):
                gaps.append({"code": "GAP_ASSESSMENT_INSUFFICIENT", "message": "目前尚未完成足夠的缺口判斷。"})
            else:
                gaps.append({"code": "EVIDENCE_NOT_ASSEMBLED", "message": "目前沒有已解析的來源內容可供比對。"})
        if not decomposition:
            gaps.append({"code": "DECOMPOSITION_EMPTY", "message": "目前還沒有足夠資訊拆解這個問題。"})
        unique: list[dict[str, str]] = []
        seen: set[str] = set()
        for gap in gaps:
            code = str(gap.get("code") or "UNKNOWN_GAP")
            if code not in seen:
                unique.append({"code": code, "message": str(gap.get("message") or "目前仍缺少必要資訊。")})
                seen.add(code)
        return unique[:8]


class SynthesisPlanner:
    """Create the bounded specification consumed by a cognitive provider."""

    _STRUCTURES: ClassVar[dict[SynthesisMode, tuple[str, ...]]] = {
        SynthesisMode.EXPLAIN: ("目前理解", "關鍵面向", "容易混淆的地方", "下一步"),
        SynthesisMode.EXPLORE: ("問題拆解", "可能方向", "目前缺口", "下一步"),
        SynthesisMode.COMPARE: ("比較基準", "相同與差異", "取捨", "建議驗證"),
        SynthesisMode.CONNECT: ("關係", "支持與衝突", "可加入圖譜的概念", "下一步"),
        SynthesisMode.CRITIQUE: ("目前主張", "風險與矛盾", "需要補證的地方", "修正方向"),
        SynthesisMode.PLAN: ("問題是什麼", "工作分解", "安全邊界", "驗證與回復"),
        SynthesisMode.RESEARCH: ("研究問題", "已有證據", "證據限制", "下一步調查"),
        SynthesisMode.SYNTHESIZE: ("目前理解", "關鍵面向", "證據與缺口", "下一步"),
    }

    def plan(
        self,
        goal: GoalInterpretation,
        decomposition: Sequence[Mapping[str, Any]],
        assembly: Mapping[str, Any],
        *,
        previous_synthesis: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        evidence = assembly.get("evidence", [])
        references = assembly.get("resource_references", [])
        gaps = assembly.get("gaps", [])
        return {
            "goal": {
                "topic": goal.topic,
                "original_objective": goal.original_objective,
                "current_objective": goal.current_objective,
                "decision": goal.decision,
            },
            "intent": goal.synthesis_mode.value,
            "scope": {
                "id": goal.scope_id,
                "title": goal.scope_title,
                "relation": goal.scope_relation.value,
                "objective_changed": goal.objective_changed,
                "parent_scope_id": goal.parent_scope_id,
            },
            "subquestions": [dict(item) for item in list(decomposition)[:8]],
            "supporting_context": [
                {
                    "semantic_type": str(item.get("semantic_type") or ""),
                    "context_role": str(item.get("context_role") or "CONCEPT"),
                    "title": str(item.get("title") or "")[:240],
                    "content": str(item.get("content") or "")[:1_200],
                }
                for item in list(assembly.get("nodes", []))[:40]
                if isinstance(item, Mapping)
            ],
            "evidence": [dict(item) for item in list(evidence)[:12] if isinstance(item, Mapping)],
            "references": [dict(item) for item in list(references)[:8] if isinstance(item, Mapping)],
            "evidence_state": str(assembly.get("evidence_state") or ("EVIDENCE_SELECTED" if evidence else "REFERENCE_ONLY" if references else "NO_SOURCE")),
            "gaps": [dict(item) for item in list(gaps)[:8] if isinstance(item, Mapping)],
            "prior_synthesis": dict(previous_synthesis) if isinstance(previous_synthesis, Mapping) else None,
            "expected_structure": list(self._STRUCTURES[goal.synthesis_mode]),
            "context_fingerprint": str(assembly.get("context_fingerprint") or ""),
            "context_receipt": dict(assembly.get("context_receipt") or {}),
            "context_budget": dict(assembly.get("context_budget") or {}),
        }
