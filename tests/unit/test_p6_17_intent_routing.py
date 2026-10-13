from __future__ import annotations

from typing import Any

import pytest

from pados.config import Settings
from pados.graph import intent_routing, service
from pados.graph.schemas import IntentRequest


@pytest.mark.parametrize(
    ("text", "intent_class", "lane"),
    [
        ("我想要了解整個DNS系統架構", "INFORMATIONAL", "COGNITIVE"),
        ("研究這個網址跟我的專案有什麼關係", "RESEARCH", "COGNITIVE"),
        ("把這個想法拆成下一步", "PLANNING", "COGNITIVE"),
        ("幫我跑測試", "EXECUTION", "EXECUTION"),
        ("幫我整理目前的系統設定", "ADMINISTRATIVE", "COGNITIVE"),
    ],
)
def test_route_intent_has_explicit_class_and_lane(text: str, intent_class: str, lane: str) -> None:
    route = intent_routing.route_intent(text)
    assert route.intent_class.value == intent_class
    assert route.lane.value == lane
    assert route.as_dict()["requires_harness"] is (lane == "EXECUTION")
    assert route.next_actions


def test_architecture_route_produces_generic_learning_plan_without_harness() -> None:
    route = intent_routing.route_intent("我想要了解整個DNS系統架構")
    plan = intent_routing.cognitive_plan(route)
    assert plan["intent_class"] == "INFORMATIONAL"
    assert plan["lane"] == "COGNITIVE"
    assert plan["run_available"] is False
    assert plan["governance_requirements"] == []
    assert [step["title"] for step in plan["steps"]][:3] == ["核心概念", "系統角色", "運作流程"]


def test_cognitive_selection_never_uses_harness_blocker(monkeypatch: pytest.MonkeyPatch) -> None:
    node: dict[str, Any] = {
        "id": "node-1",
        "semantic_type": "IDEA",
        "title": "我想要了解整個DNS系統架構",
        "content": "",
        "metadata_json": {
            "intent_class": "INFORMATIONAL",
            "intent_lane": "COGNITIVE",
            "interpreted_goal": "我想要了解整個DNS系統架構",
        },
    }
    monkeypatch.setattr(service, "selection_context", lambda *_args: {"nodes": [node], "edges": []})
    monkeypatch.setattr(service, "_workspace", lambda *_args: {"id": "workspace-1", "project_id": None})
    actions = service.selection_actions(object(), Settings.model_validate({"PADOS_ENV": "test"}), "workspace-1", ["node-1"])
    run = next(item for item in actions if item["id"] == "RUN")
    assert run["allowed"] is False
    assert run["disabled_reasons"][0]["code"] == "COGNITIVE_GOAL_NO_EXTERNAL_RUN"
    assert "Harness" not in run["disabled_reasons"][0]["message"]


def test_intent_request_rejects_empty_text() -> None:
    with pytest.raises(ValueError):
        IntentRequest(text="")
