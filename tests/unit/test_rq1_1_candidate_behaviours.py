"""Certify the two generic behaviours carried by RQ1 candidate e1d7444 (no PADOS question or document text)."""

from __future__ import annotations

import math
import random
from typing import Any

from pados.graph import retrieval
from pados.graph.context import CognitiveContextAssembler


def _node(node_id: str, title: str, content: str = "", semantic_type: str = "CONCEPT") -> dict[str, Any]:
    return {"id": node_id, "semantic_type": semantic_type, "title": title, "content": content, "metadata_json": {}}


def _assemble(nodes: list[dict[str, Any]], roots: list[str]) -> dict[str, Any]:
    return CognitiveContextAssembler().assemble(
        workspace_id="w", root_node_ids=roots, intent="THINK", action="THINK", nodes=nodes, edges=[])


# A. Non-root IDEA nodes are excluded from evidence selection.

def test_idea_exclusion_keeps_the_root_question_and_legitimate_concepts() -> None:
    nodes = [
        _node("root", "Glacier question", "how do glaciers carve valleys", semantic_type="IDEA"),
        _node("other-idea", "Glacier hypothesis", "glaciers carve valleys by plucking", semantic_type="IDEA"),
        _node("old-output", "Glacier answer", "glaciers carve valleys", semantic_type="OUTPUT"),
        _node("concept", "Glacial erosion", "glaciers carve valleys through abrasion and plucking"),
    ]
    assembled = _assemble(nodes, ["root"])

    evidence_sources = [item["source_node_id"] for item in assembled["evidence"]]
    assert evidence_sources == ["concept"]
    assert "other-idea" not in assembled["selected_node_ids"]
    assert "old-output" not in assembled["selected_node_ids"]
    assert assembled["root_node_ids"] == ["root"]
    root_record = next(node for node in assembled["nodes"] if node["id"] == "root")
    assert root_record["content"] == "how do glaciers carve valleys"
    assert "evidence_id" not in root_record
    assert all(entry["node_id"] != "other-idea" for entry in assembled["retrieval"]["top_candidates"])


def test_an_idea_selected_as_a_root_still_participates() -> None:
    nodes = [
        _node("q1", "First question", "glacier valleys", semantic_type="IDEA"),
        _node("q2", "Second question", "glacier moraines", semantic_type="IDEA"),
        _node("concept", "Moraines", "glacier moraines are ridges of debris"),
    ]
    assembled = _assemble(nodes, ["q1", "q2"])

    assert assembled["root_node_ids"] == ["q1", "q2"]
    assert {node["id"] for node in assembled["nodes"] if not node.get("evidence_id")} == {"q1", "q2"}
    assert [item["source_node_id"] for item in assembled["evidence"]] == ["concept"]


def test_rank_nodes_default_keeps_ideas_down_weighted_but_evidence_types_exclude_them() -> None:
    nodes = [_node("idea", "Glacier", "glacier", semantic_type="IDEA"), _node("concept", "Glacier", "glacier")]

    default = {item.node_id for item in retrieval.rank_nodes("glacier", nodes, root_ids=(), edges=())}
    evidence = {item.node_id for item in retrieval.rank_nodes(
        "glacier", nodes, root_ids=(), edges=(), excluded_types=retrieval.EVIDENCE_EXCLUDED_TYPES)}

    assert default == {"idea", "concept"}
    assert evidence == {"concept"}
    assert retrieval.EVIDENCE_EXCLUDED_TYPES >= {"OUTPUT", "IDEA"}


# B. Coverage in the diverse selector is IDF-weighted.

def _scored(node_id: str, terms: set[str], score: float, tokens: set[str]) -> retrieval.ScoredNode:
    return retrieval.ScoredNode(
        node_id=node_id, title=node_id, semantic_type="CONCEPT", score=score, lexical=score, reason="lexical",
        matched_terms=frozenset(terms), tokens=frozenset(tokens), text_chars=100, node={"id": node_id})


def _pool() -> list[retrieval.ScoredNode]:
    # Two contenders with equal score, equal similarity to the anchor and exactly one newly covered term each.
    # "rare" occurs in one candidate; "wide" in three. An unweighted term count ties them, and the deterministic
    # title tie-break would then pick "a-wide"; IDF-weighted coverage must pick "z-rare".
    return [
        _scored("anchor", {"common"}, 10.0, {"common", "a1", "a2", "a3", "a4"}),
        _scored("a-wide", {"common", "wide"}, 6.0, {"common", "wide", "w1", "w2", "w3", "w4"}),
        _scored("z-rare", {"common", "rare"}, 6.0, {"common", "rare", "r1", "r2", "r3", "r4"}),
        _scored("filler-1", {"common", "wide"}, 2.0, {"common", "wide", "f1", "f2", "f3", "f4"}),
        _scored("filler-2", {"common", "wide"}, 2.0, {"common", "wide", "g1", "g2", "g3", "g4"}),
    ]


def test_rare_terms_carry_more_marginal_coverage_than_ubiquitous_terms() -> None:
    pool = _pool()
    df = {"common": 5, "wide": 3, "rare": 1}
    weight = {term: math.log1p(len(pool) / count) for term, count in df.items()}
    assert weight["rare"] > weight["wide"] > weight["common"]
    contenders = [item for item in pool if item.node_id in {"a-wide", "z-rare"}]
    assert len({item.score for item in contenders}) == 1
    assert len({len(item.matched_terms - {"common"}) for item in contenders}) == 1

    selection = retrieval.select_diverse(pool, budget_chars=10_000, max_items=2)

    assert [item.node_id for item in selection.selected] == ["anchor", "z-rare"]
    assert selection.reasons["z-rare"] == "diversity"


def test_selection_favours_distinct_information_over_repeated_common_terms() -> None:
    selected = [item.node_id for item in retrieval.select_diverse(_pool(), budget_chars=10_000, max_items=3).selected]

    assert selected[:2] == ["anchor", "z-rare"]
    assert selected.index("z-rare") < selected.index("a-wide")


def test_idf_weighted_selection_is_deterministic_under_input_order() -> None:
    expected = [item.node_id for item in retrieval.select_diverse(_pool(), budget_chars=10_000, max_items=5).selected]
    for seed in range(8):
        shuffled = _pool()
        random.Random(seed).shuffle(shuffled)
        assert [item.node_id for item in retrieval.select_diverse(
            shuffled, budget_chars=10_000, max_items=5).selected] == expected
