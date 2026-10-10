# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic provider used for bounded offline acceptance.

The mock reflects the cognitive contract. It does not contain a domain
article or domain-specific answer; deterministic fixtures provide any domain
content needed by tests.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any


class MockProvider:
    async def chat(self, prompt: str, *, response_schema: Mapping[str, Any] | None = None) -> str:
        del response_schema  # the mock reflects the cognitive contract; it is never schema-constrained
        specification = _specification_from_prompt(prompt)
        goal = specification.get("goal")
        goal = goal if isinstance(goal, Mapping) else {}
        current_objective = str(goal.get("current_objective") or "目前的認知問題")
        scope = specification.get("scope")
        scope = scope if isinstance(scope, Mapping) else {}
        title = str(scope.get("title") or _first_support_title(specification) or "目前認知整理")[:240]
        mode = str(specification.get("intent") or "SYNTHESIZE")
        subquestions = specification.get("subquestions")
        subquestions = [item for item in subquestions if isinstance(item, Mapping)] if isinstance(subquestions, list) else []
        evidence = specification.get("evidence")
        evidence = [item for item in evidence if isinstance(item, Mapping)] if isinstance(evidence, list) else []
        references = specification.get("references")
        references = [item for item in references if isinstance(item, Mapping)] if isinstance(references, list) else []
        gaps = specification.get("gaps")
        gaps = [item for item in gaps if isinstance(item, Mapping)] if isinstance(gaps, list) else []
        support = specification.get("supporting_context")
        support = [item for item in support if isinstance(item, Mapping)] if isinstance(support, list) else []
        expected = specification.get("expected_structure")
        expected = [str(item) for item in expected] if isinstance(expected, list) else []

        lines = [
            f"目前正在回答：{current_objective}",
            f"認知動作：{mode}",
            "",
            "問題拆解",
        ]
        if subquestions:
            lines.extend(f"- {item.get('title') or item.get('question') or ''!s}" for item in subquestions[:8])
        else:
            support_titles = [str(item.get("title") or "") for item in support if item.get("title")]
            lines.extend(f"- {item}" for item in support_titles[:8])
        lines.extend(["", "目前理解"])
        if support:
            lines.extend(
                f"- {item.get('title') or '脈絡'!s}: {str(item.get('content') or '')[:500]}"
                for item in support[:8]
                if item.get("title") or item.get("content")
            )
        else:
            lines.append("- 目前只有問題描述，尚未形成可驗證的結論。")
        lines.extend(["", "已有證據"])
        if evidence:
            lines.extend(
                f"- {item.get('source_title') or '來源'!s}: {item.get('excerpt') or ''!s}"
                for item in evidence[:12]
            )
        else:
            lines.append("- 目前沒有已解析的來源內容。")
        evidence_source_ids = {
            str(item.get("source_id") or item.get("source_node_id"))
            for item in evidence
            if item.get("source_id") or item.get("source_node_id")
        }
        if references:
            lines.extend(["", "來源參照"])
            for reference in references[:8]:
                title_text = str(reference.get("title") or "Untitled source")
                source_id = str(reference.get("source_id") or reference.get("source_node_id") or "")
                if reference.get("content_available") and source_id in evidence_source_ids:
                    lines.append(f"- {title_text}（已取得內容並選取證據）")
                elif reference.get("content_available"):
                    lines.append(f"- {title_text}：已取得內容，但尚未取得可供分析的證據。")
                else:
                    lines.append(f"- {title_text}：已加入來源，但尚未取得可供分析的內容。")
        lines.extend(["", "還缺什麼"])
        if gaps:
            lines.extend(f"- {item.get('message') or '目前仍缺少必要資訊。'!s}" for item in gaps[:8])
        else:
            lines.append("- 目前沒有由這份合約辨識出的額外缺口。")
        lines.extend(["", "目前結論", "- 這是一份依據目前目標、拆解與可用證據形成的暫定理解，仍需人工確認。"])
        lines.extend(["", "下一步"])
        lines.extend(f"- {item}" for item in expected[:8])
        subquestion_items = [
            {
                "id": str(item.get("id") or f"mock-{index}"),
                "question": str(item.get("question") or item.get("title") or "")[:500],
            }
            for index, item in enumerate(subquestions[:8])
        ]
        gap_items = [
            {
                "code": str(item.get("code") or "MOCK_GAP")[:80],
                "message": str(item.get("message") or "")[:500],
            }
            for item in gaps[:8]
        ]
        evidence_refs = [
            str(item.get("evidence_id") or "")[:160]
            for item in evidence[:12]
            if item.get("evidence_id")
        ]
        return json.dumps(
            {
                "title": title,
                "answer": "\n".join(lines),
                "subquestions": subquestion_items,
                "gaps": gap_items,
                "claims": [],
                "evidence_refs": evidence_refs,
                "proposed_graph_changes": [],
                "limitations": ["MOCK_CONTRACT_TEST_ONLY", "人工確認前不得視為已驗證的認知結論"],
            },
            ensure_ascii=False,
        )

    async def healthcheck(self) -> dict[str, str]:
        return {"status": "ok", "provider": "mock"}

    async def estimate_context(self, prompt: str) -> int:
        return max(1, len(prompt.split()))


def _specification_from_prompt(prompt: str) -> dict[str, Any]:
    match = re.search(
        r"(?:Cognitive specification|Context assembly) JSON:\s*(\{.*\})\s*End (?:cognitive specification|context assembly)",
        prompt,
        re.DOTALL,
    )
    if match is None:
        return {}
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    if "context_assembly" in value and isinstance(value["context_assembly"], Mapping):
        return dict(value["context_assembly"])
    return value


def _first_support_title(specification: Mapping[str, Any]) -> str:
    support = specification.get("supporting_context")
    if not isinstance(support, list):
        return ""
    for item in support:
        if isinstance(item, Mapping) and item.get("title"):
            return str(item["title"])
    return ""
