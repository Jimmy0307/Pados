# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded natural-language routing for the human workspace entry point."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class IntentClass(StrEnum):
    COGNITIVE = "COGNITIVE"
    INFORMATIONAL = "INFORMATIONAL"
    RESEARCH = "RESEARCH"
    PLANNING = "PLANNING"
    EXECUTION = "EXECUTION"
    ADMINISTRATIVE = "ADMINISTRATIVE"


class IntentLane(StrEnum):
    COGNITIVE = "COGNITIVE"
    EXECUTION = "EXECUTION"


@dataclass(frozen=True)
class IntentRoute:
    intent_class: IntentClass
    lane: IntentLane
    interpreted_goal: str
    confidence: str
    next_actions: tuple[dict[str, str], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "intent_class": self.intent_class.value,
            "lane": self.lane.value,
            "interpreted_goal": self.interpreted_goal,
            "confidence": self.confidence,
            "next_actions": list(self.next_actions),
            "requires_harness": self.lane == IntentLane.EXECUTION,
        }


_EXECUTION_PATTERNS = (
    r"\b(run|execute|modify|create|delete|deploy|write|scan|workflow|test|repository)\b",
    r"執行|修改|建立.*檔案|建立一個檔案|跑測試|執行.*workflow|掃描.*設定|部署|刪除|寫入|改.*repository|修改.*repo",
)
_ADMIN_PATTERNS = (r"\b(admin|settings|configure|configuration|database|provider)\b", r"管理|設定系統|系統設定|資料庫|供應商|權限")
_PLANNING_PATTERNS = (r"plan|break.*down|next step|roadmap|learning path|research path", r"規劃|拆成下一步|拆解|學習路線|研究路線|問題拆解|下一步")
_RESEARCH_PATTERNS = (r"research|compare|find|source|article|paper|url|relationship", r"研究|比較|找相關|資料|文章|論文|網址|這篇.*關係|支持與衝突")
_INFORMATIONAL_PATTERNS = (r"understand|explain|how does|what is|learn|overview", r"了解|理解|解釋|怎麼運作|如何|什麼是|學習|架構")


def _matches(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _goal(text: str) -> str:
    normalized = " ".join(text.strip().split())
    if len(normalized) <= 240:
        return normalized
    return f"{normalized[:237]}..."


def _actions(intent_class: IntentClass) -> tuple[dict[str, str], ...]:
    if intent_class in {IntentClass.COGNITIVE, IntentClass.INFORMATIONAL}:
        return (
            {"id": "EXPLAIN", "label": "深入思考"},
            {"id": "EXPAND", "label": "延伸概念"},
            {"id": "SEARCH", "label": "找相關資料"},
            {"id": "PLAN", "label": "建立學習路線"},
            {"id": "PAUSE", "label": "先放著"},
        )
    if intent_class == IntentClass.RESEARCH:
        return (
            {"id": "EXPLAIN", "label": "整理目前理解"},
            {"id": "SEARCH", "label": "找相關資料"},
            {"id": "EXPAND", "label": "延伸概念"},
            {"id": "PLAN", "label": "建立研究路線"},
            {"id": "PAUSE", "label": "先放著"},
        )
    if intent_class == IntentClass.PLANNING:
        return (
            {"id": "PLAN", "label": "拆成下一步"},
            {"id": "EXPLAIN", "label": "釐清問題"},
            {"id": "EXPAND", "label": "補充可能遺漏"},
            {"id": "PAUSE", "label": "先放著"},
        )
    if intent_class == IntentClass.EXECUTION:
        return (
            {"id": "PLAN", "label": "先檢視執行計畫"},
            {"id": "PAUSE", "label": "先放著"},
        )
    return (
        {"id": "EXPLAIN", "label": "說明目前狀態"},
        {"id": "PLAN", "label": "整理處理路線"},
        {"id": "PAUSE", "label": "先放著"},
    )


def route_intent(text: str) -> IntentRoute:
    """Classify ordinary input with conservative, deterministic precedence."""

    goal = _goal(text)
    if not goal:
        return IntentRoute(IntentClass.COGNITIVE, IntentLane.COGNITIVE, "", "LOW", _actions(IntentClass.COGNITIVE))
    if _matches(goal, _EXECUTION_PATTERNS):
        intent_class = IntentClass.EXECUTION
    elif _matches(goal, _ADMIN_PATTERNS):
        intent_class = IntentClass.ADMINISTRATIVE
    elif _matches(goal, _PLANNING_PATTERNS):
        intent_class = IntentClass.PLANNING
    elif _matches(goal, _RESEARCH_PATTERNS):
        intent_class = IntentClass.RESEARCH
    elif _matches(goal, _INFORMATIONAL_PATTERNS):
        intent_class = IntentClass.INFORMATIONAL
    else:
        intent_class = IntentClass.COGNITIVE
    lane = IntentLane.EXECUTION if intent_class == IntentClass.EXECUTION else IntentLane.COGNITIVE
    return IntentRoute(intent_class, lane, goal, "HIGH" if intent_class != IntentClass.COGNITIVE else "MEDIUM", _actions(intent_class))


def cognitive_plan(route: IntentRoute) -> dict[str, object]:
    """Return a bounded human-readable route, never an execution plan.

    Domain-specific decomposition belongs to deterministic test fixtures or a
    provider's structured contract.  The product route only exposes general
    reasoning dimensions inferred from the goal.
    """

    goal = route.interpreted_goal
    if route.intent_class == IntentClass.RESEARCH:
        steps = ["界定問題與關鍵詞", "找可信資料", "整理主要觀點", "比較支持與衝突", "留下可追溯的結論"]
    elif route.intent_class == IntentClass.PLANNING:
        steps = ["釐清目標", "拆成可處理的問題", "排序下一步", "確認完成條件"]
    elif re.search(r"architecture|system|架構|系統", goal, re.IGNORECASE):
        steps = ["核心概念", "系統角色", "運作流程", "風險與限制", "驗證方式", "下一步"]
    else:
        steps = ["先說清楚核心概念", "找出關鍵組成", "補上容易遺漏的部分", "整理成可回看的輸出"]
    return {
        "plan_kind": "LEARNING_ROUTE" if route.intent_class in {IntentClass.COGNITIVE, IntentClass.INFORMATIONAL} else "RESEARCH_ROUTE" if route.intent_class == IntentClass.RESEARCH else "PROBLEM_DECOMPOSITION",
        "interpreted_goal": goal,
        "intent_class": route.intent_class.value,
        "lane": route.lane.value,
        "steps": [{"sequence": index, "title": step, "kind": "THINK"} for index, step in enumerate(steps, start=1)],
        "run_available": False,
        "governance_requirements": [],
        "blockers": [],
        "expected_output": "概念、問題拆解與可回看的思考結果會回到心智圖。",
    }
