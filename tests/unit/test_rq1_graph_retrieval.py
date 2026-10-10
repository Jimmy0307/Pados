from __future__ import annotations

import random
from typing import Any

import pytest

from pados.graph import context, repository, retrieval
from pados.graph.context import CognitiveContextAssembler


def _node(node_id: str, title: str, content: str = "", semantic_type: str = "CONCEPT") -> dict[str, Any]:
    return {"id": node_id, "semantic_type": semantic_type, "title": title, "content": content, "metadata_json": {}}


def _edge(source: str, target: str) -> dict[str, Any]:
    return {"id": f"{source}-{target}", "source_node_id": source, "target_node_id": target}


def test_tokenizer_lowercases_drops_stopwords_and_folds_inflections() -> None:
    tokens = retrieval.tokenize("The Resolver RESOLVED what the models resolve, and the Commits were committed.")

    assert "the" not in tokens and "what" not in tokens and "and" not in tokens
    assert tokens.count("resolv") == 3
    assert tokens.count("commit") == 2
    assert "model" in tokens
    assert retrieval.tokenize("plane") != retrieval.tokenize("plan")


def test_tokenizer_splits_cjk_runs_into_character_bigrams() -> None:
    assert retrieval.tokenize("資料來源") == ["資料", "料來", "來源"]
    assert retrieval.tokenize("DNS安全") == ["dns", "安全"]
    assert retrieval.tokenize("模") == ["模"]


def test_cjk_query_retrieves_cjk_content() -> None:
    ranked = retrieval.rank_nodes(
        "如何設定儲存空間？",
        [_node("a", "網路設定", "防火牆規則與連接埠"), _node("b", "儲存空間", "儲存空間的預算與壓力等級")],
        root_ids=(),
        edges=(),
    )

    assert ranked[0].node_id == "b"
    assert ranked[0].score > ranked[1].score


def test_bm25_title_match_outranks_the_same_term_in_content() -> None:
    ranked = retrieval.rank_nodes(
        "eviction policy",
        [
            _node("body", "Overview", "eviction policy is described here among many other words about storage"),
            _node("heading", "Eviction policy", "described here among many other words about storage"),
            _node("none", "Unrelated", "nothing relevant in this section"),
        ],
        root_ids=(),
        edges=(),
    )

    assert [item.node_id for item in ranked][:2] == ["heading", "body"]
    assert ranked[-1].score == 0


def test_contiguous_phrase_scores_above_scattered_terms() -> None:
    ranked = retrieval.rank_nodes(
        "control plane",
        [
            _node("scattered", "A", "the plane of travel; access control is elsewhere"),
            _node("phrase", "B", "the control plane owns intent and policy"),
        ],
        root_ids=(),
        edges=(),
    )

    assert ranked[0].node_id == "phrase"


def test_graph_local_expansion_reaches_a_continuation_section() -> None:
    nodes = [
        _node("hit", "Storage pressure levels", "storage pressure levels ok warn critical"),
        _node("cont", "Continuation", "the remaining rows of the table follow"),
        _node("far", "Elsewhere", "an unrelated paragraph"),
    ]
    ranked = {item.node_id: item for item in retrieval.rank_nodes(
        "storage pressure levels", nodes, root_ids=(), edges=[_edge("hit", "cont")])}

    assert ranked["cont"].reason == "graph_local"
    assert ranked["cont"].score == pytest.approx(retrieval.GRAPH_ALPHA * ranked["hit"].score, rel=1e-4)
    assert ranked["far"].score == 0


def test_root_neighbours_get_alpha_treatment_not_a_fixed_score() -> None:
    nodes = [
        _node("root", "Question about storage", "storage pressure", semantic_type="IDEA"),
        _node("hit", "Storage pressure", "storage pressure levels"),
        _node("linked", "Linked note", "no shared words at all"),
    ]
    ranked = {item.node_id: item for item in retrieval.rank_nodes(
        "Question about storage storage pressure", nodes, root_ids=["root"], edges=[_edge("root", "linked")])}

    assert "root" not in ranked
    assert ranked["linked"].reason == "graph_local"
    assert ranked["linked"].score == pytest.approx(retrieval.GRAPH_ALPHA * ranked["hit"].score, rel=1e-4)
    assert ranked["linked"].score < ranked["hit"].score


def test_outputs_are_excluded_and_non_root_ideas_are_down_weighted() -> None:
    nodes = [
        _node("concept", "Storage pressure", "storage pressure"),
        _node("idea", "Storage pressure", "storage pressure", semantic_type="IDEA"),
        _node("output", "Storage pressure", "storage pressure", semantic_type="OUTPUT"),
    ]
    ranked = {item.node_id: item for item in retrieval.rank_nodes("storage pressure", nodes, root_ids=(), edges=())}

    assert "output" not in ranked
    assert ranked["idea"].score == pytest.approx(ranked["concept"].score * retrieval.NON_ROOT_IDEA_WEIGHT, rel=1e-4)


def test_mmr_prefers_a_distinct_relevant_chunk_over_a_near_duplicate() -> None:
    duplicate = "storage pressure levels ok warn critical emergency budget eviction recommendation advisory snapshot"
    nodes = [
        _node("dup-a", "Storage pressure levels", duplicate),
        _node("dup-b", "Storage pressure levels (copy)", duplicate + " copy"),
        _node("distinct", "Storage modes", "storage modes portable managed external volumes pressure"),
    ]
    ranked = retrieval.rank_nodes("storage pressure levels", nodes, root_ids=(), edges=())
    assert [item.node_id for item in ranked][:2] == ["dup-a", "dup-b"]

    selection = retrieval.select_diverse(ranked, budget_chars=10_000, max_items=2)

    assert [item.node_id for item in selection.selected] == ["dup-a", "distinct"]
    assert selection.reasons["distinct"] == "diversity"


def test_near_duplicates_are_dropped_with_a_reason() -> None:
    text = "storage pressure levels ok warn critical"
    ranked = retrieval.rank_nodes(
        "storage pressure", [_node("a", "Storage pressure", text), _node("b", "Storage pressure", text)],
        root_ids=(), edges=())
    selection = retrieval.select_diverse(ranked, budget_chars=10_000, max_items=5)

    assert [item.node_id for item in selection.selected] == ["a"]
    assert ("b", "near_duplicate") in [(item.node_id, reason) for item, reason in selection.dropped]


def test_selection_respects_the_character_budget() -> None:
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    nodes = [_node(f"n{index}", f"Storage note {word}", f"storage pressure {word} " + "x" * 900) for index, word in enumerate(words)]
    ranked = retrieval.rank_nodes("storage pressure", nodes, root_ids=(), edges=())
    selection = retrieval.select_diverse(ranked, budget_chars=2_000, max_items=6)

    assert 1 <= len(selection.selected) <= 2
    assert selection.used_chars <= 2_000
    assert any(reason == "budget" for _, reason in selection.dropped)


def test_ranking_and_selection_are_deterministic_under_input_order() -> None:
    nodes = [_node(f"n{index:02d}", f"Section {index % 3}", "shared storage term") for index in range(12)]
    expected = retrieval.rank_nodes("storage", nodes, root_ids=(), edges=())
    for seed in range(5):
        shuffled = list(nodes)
        random.Random(seed).shuffle(shuffled)
        ranked = retrieval.rank_nodes("storage", shuffled, root_ids=(), edges=())
        assert [item.node_id for item in ranked] == [item.node_id for item in expected]
        assert [item.node_id for item in retrieval.select_diverse(ranked, budget_chars=500, max_items=3).selected] == [
            item.node_id for item in retrieval.select_diverse(expected, budget_chars=500, max_items=3).selected
        ]
    assert [item.title for item in expected[:4]] == ["Section 0", "Section 0", "Section 0", "Section 0"]


def test_diagnostics_state_lexical_mode_and_no_semantic_capability() -> None:
    assembled = CognitiveContextAssembler().assemble(
        workspace_id="w",
        root_node_ids=["root"],
        intent="THINK",
        action="THINK",
        nodes=[
            _node("root", "Storage question", "describe storage pressure levels", semantic_type="IDEA"),
            _node("hit", "Storage pressure", "storage pressure levels"),
            _node("miss", "Network", "firewall ports"),
        ],
        edges=[],
    )
    diagnostics = assembled["retrieval"]

    assert diagnostics["mode"] == "LEXICAL_BM25_GRAPH_LOCAL"
    assert diagnostics["semantic"] == "UNAVAILABLE_NO_EMBEDDING_CAPABILITY"
    assert "storag" in diagnostics["query_terms"]
    assert diagnostics["top_candidates"][0]["node_id"] == "hit"
    assert diagnostics["selected"] == [diagnostics["selected"][0]]
    assert diagnostics["selected"][0]["reason"] == "lexical"
    assert diagnostics["budget"]["chars"] > 0
    assert "vector" not in str(diagnostics).casefold()


def test_fit_records_truncates_an_1100_character_record_instead_of_dropping_it() -> None:
    record = {"id": "long", "content": "x" * 1_100}
    selected, remaining, truncated = context._fit_records([record], budget=240, text_key="content")

    assert [item["id"] for item in selected] == ["long"]
    assert truncated is True
    assert remaining >= 0
    assert context._estimate_tokens(selected[0]["content"]) <= 240
    assert len(selected[0]["content"]) > 900


@pytest.mark.parametrize("budget", [1, 10, 49, 50, 51, 100, 240, 275, 276, 1_000])
def test_fit_records_never_exceeds_the_budget(budget: int) -> None:
    records = [{"id": str(index), "content": "y" * length} for index, length in enumerate((1_100, 30, 700, 5))]
    selected, remaining, _ = context._fit_records(records, budget=budget, text_key="content")

    used = sum(context._estimate_tokens(item["content"]) for item in selected if item["content"])
    assert used <= budget
    assert remaining == budget - used
    assert all(len(item["content"]) >= 30 or item["id"] == "3" for item in selected)


def test_long_concept_chunk_reaches_the_evidence_segment() -> None:
    body = "storage pressure levels " + "detail " * 160
    assert len(body) > 1_000
    assembled = CognitiveContextAssembler().assemble(
        workspace_id="w",
        root_node_ids=["root"],
        intent="THINK",
        action="THINK",
        nodes=[_node("root", "Q", "storage pressure levels", semantic_type="IDEA"), _node("chunk", "Storage", body)],
        edges=[],
    )

    assert [item["source_node_id"] for item in assembled["evidence"]] == ["chunk"]
    assert len(assembled["evidence"][0]["excerpt"]) >= 1_000


def test_graph_search_ranks_prefiltered_rows_and_keeps_single_word_phrase_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    rows = [
        _node("recent", "Note", "mentions storage once"),
        _node("best", "Storage pressure", "storage pressure levels"),
        _node("substring", "Misc", "nonstoragexyz"),
    ]

    def fake_many(_session: Any, _sql: str, values: dict[str, Any]) -> list[dict[str, Any]]:
        calls.append(values)
        return list(rows)

    monkeypatch.setattr(repository, "_many", fake_many)

    single = repository.search_nodes(object(), "w", "storage", 10)  # type: ignore[arg-type]
    assert calls[-1]["patterns"] == ["%storage%"]
    assert calls[-1]["prefilter"] == 2_000
    assert {row["id"] for row in single} == {"recent", "best", "substring"}
    assert single[-1]["id"] == "substring"

    multi = repository.search_nodes(object(), "w", "storage pressure levels", 2)  # type: ignore[arg-type]
    assert "%storage pressure levels%" in calls[-1]["patterns"]
    assert {"%storage%", "%pressure%", "%levels%"} <= set(calls[-1]["patterns"])
    assert [row["id"] for row in multi] == ["best", "recent"]
