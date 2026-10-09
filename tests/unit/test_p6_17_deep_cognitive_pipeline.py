from __future__ import annotations

import asyncio
import json

from pados.graph.cognition import build_prompt
from pados.graph.cognitive_pipeline import (
    CognitiveGoalInterpreter,
    EvidenceResolver,
    GapDetector,
    ProblemDecomposer,
    ScopeRelation,
    SynthesisPlanner,
)
from pados.graph.context import CognitiveContextAssembler
from pados.graph.schemas import CognitiveOperation
from pados.providers.mock import MockProvider


def test_goal_interpreter_keeps_refinement_in_scope_and_links_expansion() -> None:
    interpreter = CognitiveGoalInterpreter()
    first = interpreter.interpret("Understand the service architecture", intent_class="INFORMATIONAL")
    refinement = interpreter.interpret(
        "Understand the service architecture",
        intent_class="INFORMATIONAL",
        prior_goals=[first.as_dict()],
    )
    expanded = interpreter.interpret(
        "How can AI safely help deploy and maintain the service?",
        intent_class="INFORMATIONAL",
        prior_goals=[first.as_dict()],
    )

    assert refinement.scope_relation == ScopeRelation.SAME_SCOPE_UPDATE
    assert refinement.scope_id == first.scope_id
    assert expanded.scope_relation == ScopeRelation.SCOPE_EXPANSION
    assert expanded.scope_id != first.scope_id
    assert expanded.parent_scope_id == first.scope_id
    assert expanded.objective_changed is True
    assert expanded.scope_title != first.scope_title
    assert "AI 輔助" not in expanded.scope_title
    assert "目前正在回答" not in expanded.scope_title


def test_related_topic_and_security_question_do_not_create_a_silent_new_objective() -> None:
    interpreter = CognitiveGoalInterpreter()
    first = interpreter.interpret("Understand DNS system architecture", intent_class="INFORMATIONAL")
    update = interpreter.interpret("DNSSEC", intent_class="INFORMATIONAL", prior_goals=[first.as_dict()])
    security = interpreter.interpret("DNS security risks", intent_class="INFORMATIONAL", prior_goals=[first.as_dict()])

    assert update.scope_relation in {ScopeRelation.SAME_SCOPE_UPDATE, ScopeRelation.SUBQUESTION}
    assert security.scope_relation in {ScopeRelation.SUBQUESTION, ScopeRelation.SCOPE_EXPANSION}


def test_problem_decomposer_is_generic_and_bounded_for_deployment_questions() -> None:
    goal = CognitiveGoalInterpreter().interpret(
        "How can AI safely help deploy and maintain the service?",
        intent_class="INFORMATIONAL",
    )
    questions = ProblemDecomposer().decompose(goal)

    assert len(questions) == 8
    assert {item["id"] for item in questions} == {
        "architecture", "configuration", "security", "ai_reliability",
        "validation", "governance", "monitoring", "rollback",
    }
    assert all(item["kind"] == "SUBQUESTION" for item in questions)


def test_problem_decomposer_titles_carry_input_semantics() -> None:
    goal = CognitiveGoalInterpreter().interpret(
        "DNS 如果拿來當 AI 的防範機制或 sandbox，這個方向有沒有可能？",
        intent_class="INFORMATIONAL",
    )
    questions = ProblemDecomposer().decompose(goal)

    generic_titles = {"核心概念", "關鍵面向", "可用證據", "未知與限制", "下一步"}
    assert questions
    assert all(item["title"] not in generic_titles for item in questions)
    assert all(goal.current_objective in item["question"] for item in questions)


def test_evidence_resolver_distinguishes_reference_from_content() -> None:
    nodes = [
        {
            "id": "available",
            "semantic_type": "RESOURCE",
            "title": "Available source",
            "content": "Grounded paragraph about validation and rollback.",
            "metadata_json": {
                "retrieval_status": "FETCHED",
                "evidence_eligibility": "EVIDENCE_ELIGIBLE",
                "final_url": "https://example.com/available",
                "extracted_text_hash": "hash-available",
            },
        },
        {
            "id": "unavailable",
            "semantic_type": "RESOURCE",
            "title": "Unavailable source",
            "content": "",
            "metadata_json": {
                "retrieval_status": "UNSUPPORTED",
                "original_url": "https://example.com/unavailable",
            },
        },
    ]
    resolved = EvidenceResolver().resolve(nodes, query="validation")

    assert len(resolved["references"]) == 2
    assert {item["content_available"] for item in resolved["references"]} == {True, False}
    assert len(resolved["evidence"]) == 1
    assert resolved["evidence"][0]["excerpt"] == "Grounded paragraph about validation and rollback."
    assert resolved["evidence"][0]["provenance"]["content_hash"] == "hash-available"
    assert resolved["gaps"][0]["message"] == "已加入來源，但尚未取得可供分析的內容。"


def test_assembler_exposes_bounded_evidence_and_gaps() -> None:
    assembled = CognitiveContextAssembler().assemble(
        workspace_id="workspace",
        root_node_ids=["idea"],
        intent="PLAN",
        action="PLAN",
        nodes=[
            {"id": "idea", "semantic_type": "IDEA", "title": "Deploy service", "content": ""},
            {
                "id": "resource",
                "semantic_type": "RESOURCE",
                "title": "Reference",
                    "content": "A source paragraph about deploying the service.",
                "metadata_json": {
                    "retrieval_status": "FETCHED",
                    "evidence_eligibility": "EVIDENCE_ELIGIBLE",
                    "final_url": "https://example.com",
                },
            },
        ],
        edges=[],
    )

    assert assembled["source_summary"]["references"] == 1
    assert assembled["source_summary"]["evidence_units"] == 1
    assert assembled["resource_references"][0]["kind"] == "REFERENCE"
    assert assembled["evidence"][0]["kind"] == "EVIDENCE"
    assert assembled["evidence"][0]["source_node_id"] == "resource"


def test_context_excludes_historical_outputs_and_deduplicates_resources() -> None:
    assembled = CognitiveContextAssembler().assemble(
        workspace_id="workspace",
        root_node_ids=["idea"],
        intent="THINK",
        action="THINK",
        nodes=[
            {"id": "idea", "semantic_type": "IDEA", "title": "DNS security", "content": "question"},
            {
                "id": "resource-a",
                "semantic_type": "RESOURCE",
                "title": "DNS source",
                "content": "DNS security source paragraph.",
                "metadata_json": {
                    "retrieval_status": "FETCHED",
                    "evidence_eligibility": "EVIDENCE_ELIGIBLE",
                    "extracted_text_hash": "same-content",
                    "final_url": "https://example.com/dns",
                },
            },
            {
                "id": "resource-b",
                "semantic_type": "RESOURCE",
                "title": "Duplicate DNS source",
                "content": "DNS security source paragraph.",
                "metadata_json": {
                    "retrieval_status": "FETCHED",
                    "evidence_eligibility": "EVIDENCE_ELIGIBLE",
                    "extracted_text_hash": "same-content",
                    "final_url": "https://example.com/dns-copy",
                },
            },
            {
                "id": "historical-output",
                "semantic_type": "OUTPUT",
                "title": "DNS 系統架構總覽",
                "content": "prior generated synthesis",
                "metadata_json": {"result_type": "COGNITIVE_RESULT"},
            },
        ],
        edges=[],
        previous_synthesis={"id": "prior", "version": 2, "title": "Prior", "content": "bounded prior"},
    )

    prompt_ids = {item["id"] for item in assembled["nodes"]}
    assert "historical-output" not in prompt_ids
    assert len(assembled["evidence"]) == 1
    assert assembled["previous_synthesis"]["id"] == "prior"
    assert all(item["context_role"] != "HISTORICAL_OUTPUT" for item in assembled["nodes"])


def test_gap_detection_is_unknown_when_evidence_sufficiency_is_unestablished() -> None:
    goal = CognitiveGoalInterpreter().interpret(
        "Can AI use DNS as a sandbox defense mechanism?",
        intent_class="INFORMATIONAL",
    )
    gaps = GapDetector().detect(
        goal,
        ProblemDecomposer().decompose(goal),
        {"references": [{"kind": "REFERENCE"}], "evidence": [], "gaps": []},
    )

    assert any(item["code"] == "GAP_ASSESSMENT_INSUFFICIENT" for item in gaps)


def test_planner_and_mock_change_structure_with_new_subquestions() -> None:
    interpreter = CognitiveGoalInterpreter()
    goal = interpreter.interpret("How can AI safely help deploy and maintain the service?", intent_class="INFORMATIONAL")
    decomposition = ProblemDecomposer().decompose(goal)
    assembly = {
        "context_fingerprint": "fingerprint",
        "nodes": [{"semantic_type": "IDEA", "title": "service", "content": "current context"}],
        "evidence": [],
        "resource_references": [],
        "gaps": GapDetector().detect(goal, decomposition, {"references": [], "evidence": [], "gaps": []}),
    }
    specification = SynthesisPlanner().plan(goal, decomposition, assembly)
    response = json.loads(asyncio.run(MockProvider().chat(build_prompt(CognitiveOperation.THINK, {"synthesis_spec": specification}))))

    content = response["answer"]
    assert response["title"] == goal.scope_title
    assert "架構與角色" in content
    assert "安全與權限" in content
    assert "目前正在回答" in content
    assert "目前沒有已解析的來源內容" in content
    assert len(json.dumps(specification, ensure_ascii=False)) < 20_000
