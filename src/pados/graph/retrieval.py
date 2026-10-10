# SPDX-License-Identifier: AGPL-3.0-only
"""Pure, deterministic lexical retrieval over graph nodes (no database, no I/O).

Ranking is Okapi BM25 over title + content, with title terms weighted (heading match), a bonus
for contiguous query phrases, and graph-local expansion along one-hop edges. Selection is an
MMR-style diverse pick under a character budget. No embedding capability exists in the graph path,
so the diagnostics state ``semantic = UNAVAILABLE_NO_EMBEDDING_CAPABILITY``; no placeholder vectors
are ever used.

Nothing here knows any question, document, heading or node title: every constant is generic.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

RETRIEVAL_MODE: Final = "LEXICAL_BM25_GRAPH_LOCAL"
SEMANTIC_STATUS: Final = "UNAVAILABLE_NO_EMBEDDING_CAPABILITY"

BM25_K1: Final = 1.2
BM25_B: Final = 0.75
TITLE_WEIGHT: Final = 3
PREFIX_MATCH_WEIGHT: Final = 0.5
PREFIX_MIN_CHARS: Final = 3
PHRASE_BONUS: Final = 0.5
GRAPH_ALPHA: Final = 0.35
GRAPH_EXPANSION_HITS: Final = 10
NON_ROOT_IDEA_WEIGHT: Final = 0.3
MMR_LAMBDA: Final = 0.7
COVERAGE_BONUS: Final = 0.15
NEAR_DUPLICATE_SIMILARITY: Final = 0.8
LOW_SCORE_FRACTION: Final = 0.1
MIN_TRUNCATED_CHARS: Final = 200
MAX_QUERY_TERMS: Final = 64
DIAGNOSTIC_TOP: Final = 20
DIAGNOSTIC_DROPPED: Final = 10

DEFAULT_EXCLUDED_TYPES: Final = frozenset({"OUTPUT"})
EVIDENCE_EXCLUDED_TYPES: Final = frozenset({"OUTPUT", "IDEA"})
DEFAULT_TYPE_WEIGHTS: Final[Mapping[str, float]] = {"IDEA": NON_ROOT_IDEA_WEIGHT}

# A small, generic English function-word list (no domain vocabulary).
STOPWORDS: Final = frozenset(
    """
    a an the and or but nor if then else of to in on at by for with from as into onto about over under
    is are was were be been being am it its this that these those there here their theirs them they
    he him his she her hers we us our ours you your yours i me my mine do does did doing done not no
    so such than too very can cannot could will would should shall may might must what which who whom
    whose when where why how all any each every both few more most other some only own same again
    further also just using use used via per while during before after above below up down out off
    through between against because until once
    """.split()
)

_WORD = re.compile(r"[^\W_]+")


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0x3040 <= code <= 0x30FF
        or 0xAC00 <= code <= 0xD7AF
        or 0x20000 <= code <= 0x2FA1F
    )


_STEP4_SUFFIXES: Final = (
    "ement", "ance", "ence", "able", "ible", "ment", "ant", "ent", "ism", "ate", "iti", "ous", "ive", "ize",
    "al", "er", "ic",
)


def _vowel(word: str, index: int) -> bool:
    char = word[index]
    if char in "aeiou":
        return True
    return char == "y" and index > 0 and not _vowel(word, index - 1)


def _measure(stem: str) -> int:
    """Porter's m: the number of vowel-consonant sequences in ``stem``."""

    pattern = "".join("v" if _vowel(stem, index) else "c" for index in range(len(stem)))
    return re.sub(r"(.)\1+", r"\1", pattern).count("vc")


def _has_vowel(stem: str) -> bool:
    return any(_vowel(stem, index) for index in range(len(stem)))


def _cvc(stem: str) -> bool:
    return (
        len(stem) >= 3
        and not _vowel(stem, len(stem) - 3)
        and _vowel(stem, len(stem) - 2)
        and not _vowel(stem, len(stem) - 1)
        and stem[-1] not in "wxy"
    )


def _stem(token: str) -> str:
    """A compact Porter-style stemmer (inflections, common derivational endings, final ``e``).

    Generic English morphology only (no dictionary, no domain terms): resolve / resolved / resolver,
    commit / committed, model / models / modelling share a stem, while short words such as ``plane``
    or ``never`` are kept apart from ``plan`` or ``nev``.
    """

    if not token.isascii() or not token.isalpha() or len(token) <= 3:
        return token
    word = token
    if word.endswith("sses"):
        word = word[:-2]
    elif word.endswith("ies"):
        word = word[:-2]
    elif word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    for suffix in ("ing", "ed"):
        stem = word[: -len(suffix)]
        if word.endswith(suffix) and _has_vowel(stem):
            word = stem
            if word.endswith(("at", "bl", "iz")):
                word += "e"
            elif len(word) > 1 and word[-1] == word[-2] and word[-1] not in "lsz":
                word = word[:-1]
            elif _measure(word) == 1 and _cvc(word):
                word += "e"
            break
    if word.endswith("y") and _has_vowel(word[:-1]):
        word = word[:-1] + "i"
    for suffix in _STEP4_SUFFIXES:
        if word.endswith(suffix) and _measure(word[: -len(suffix)]) > 1:
            word = word[: -len(suffix)]
            break
    if word.endswith("ion") and _measure(word[:-3]) > 1 and word[:-3].endswith(("s", "t")):
        word = word[:-3]
    if word.endswith("e"):
        stem = word[:-1]
        if _measure(stem) > 1 or (_measure(stem) == 1 and not _cvc(stem)):
            word = stem
    if word.endswith("ll") and _measure(word) > 1:
        word = word[:-1]
    return word


def _segments(run: str) -> Iterable[tuple[bool, str]]:
    current: list[str] = []
    current_cjk: bool | None = None
    for char in run:
        cjk = _is_cjk(char)
        if current_cjk is not None and cjk != current_cjk:
            yield current_cjk, "".join(current)
            current = []
        current.append(char)
        current_cjk = cjk
    if current and current_cjk is not None:
        yield current_cjk, "".join(current)


def tokenize(text: str) -> list[str]:
    """Lower-cased Unicode word tokens; stopwords dropped; CJK runs split into character bigrams."""

    tokens: list[str] = []
    normalised = unicodedata.normalize("NFKC", text or "").casefold()
    for run in _WORD.findall(normalised):
        for cjk, segment in _segments(run):
            if cjk:
                if len(segment) == 1:
                    tokens.append(segment)
                else:
                    tokens.extend(segment[index : index + 2] for index in range(len(segment) - 1))
                continue
            if len(segment) < 2 or segment in STOPWORDS:
                continue
            tokens.append(_stem(segment))
    return tokens


def query_terms(text: str) -> list[str]:
    """Distinct query terms in first-occurrence order (bounded)."""

    return list(dict.fromkeys(tokenize(text)))[:MAX_QUERY_TERMS]


def prefilter_patterns(text: str, *, limit: int = 16) -> list[str]:
    """Raw (unstemmed) query words and CJK bigrams suitable for a substring prefilter."""

    raw: list[str] = []
    normalised = unicodedata.normalize("NFKC", text or "").casefold()
    for run in _WORD.findall(normalised):
        for cjk, segment in _segments(run):
            if cjk:
                raw.extend(
                    [segment]
                    if len(segment) == 1
                    else [segment[index : index + 2] for index in range(len(segment) - 1)]
                )
            elif len(segment) >= 2 and segment not in STOPWORDS:
                raw.append(segment)
                stem = _stem(segment)
                if stem != segment:
                    raw.append(stem)
    return list(dict.fromkeys(raw))[:limit]


@dataclass(frozen=True)
class ScoredNode:
    node_id: str
    title: str
    semantic_type: str
    score: float
    lexical: float
    reason: str
    matched_terms: frozenset[str]
    tokens: frozenset[str]
    text_chars: int
    node: Mapping[str, Any]


@dataclass(frozen=True)
class Selection:
    selected: tuple[ScoredNode, ...]
    reasons: Mapping[str, str]
    dropped: tuple[tuple[ScoredNode, str], ...]
    budget_chars: int
    used_chars: int


@dataclass(frozen=True)
class _Doc:
    node_id: str
    tf: Counter[str]
    length: float
    title_tokens: tuple[str, ...]
    content_tokens: tuple[str, ...]


def _doc(node_id: str, node: Mapping[str, Any]) -> _Doc:
    title_tokens = tuple(tokenize(str(node.get("title") or "")))
    content_tokens = tuple(tokenize(str(node.get("content") or "")))
    tf: Counter[str] = Counter(content_tokens)
    for token in title_tokens:
        tf[token] += TITLE_WEIGHT
    return _Doc(node_id, tf, float(len(content_tokens) + TITLE_WEIGHT * len(title_tokens)), title_tokens,
                content_tokens)


def _term_frequency(doc: _Doc, term: str) -> float:
    exact = float(doc.tf.get(term, 0))
    if len(term) < PREFIX_MIN_CHARS or not term.isascii():
        return exact
    partial = sum(count for token, count in doc.tf.items() if token != term and token.startswith(term))
    return exact + PREFIX_MATCH_WEIGHT * partial


def _contains_pair(tokens: Sequence[str], first: str, second: str) -> bool:
    return any(tokens[index] == first and tokens[index + 1] == second for index in range(len(tokens) - 1))


def _idf(count: int, df: int) -> float:
    return math.log(1.0 + (count - df + 0.5) / (df + 0.5))


def _bm25(
    weights: Mapping[str, float],
    phrase_pairs: Sequence[tuple[str, str]],
    docs: Sequence[_Doc],
) -> dict[str, tuple[float, frozenset[str]]]:
    """Okapi BM25 over title-weighted term frequencies, plus a contiguous query-phrase bonus."""

    if not docs or not weights:
        return {doc.node_id: (0.0, frozenset()) for doc in docs}
    count = len(docs)
    average = sum(doc.length for doc in docs) / count or 1.0
    frequencies = {doc.node_id: {term: _term_frequency(doc, term) for term in weights} for doc in docs}
    idf = {
        term: _idf(count, sum(1 for doc in docs if frequencies[doc.node_id][term] > 0)) for term in weights
    }
    result: dict[str, tuple[float, frozenset[str]]] = {}
    for doc in docs:
        score = 0.0
        matched: set[str] = set()
        norm = BM25_K1 * (1.0 - BM25_B + BM25_B * doc.length / average)
        for term, weight in weights.items():
            frequency = frequencies[doc.node_id][term]
            if frequency <= 0:
                continue
            matched.add(term)
            score += weight * idf[term] * frequency * (BM25_K1 + 1.0) / (frequency + norm)
        for first, second in phrase_pairs:
            bonus = PHRASE_BONUS * (idf[first] + idf[second]) / 2.0
            if _contains_pair(doc.title_tokens, first, second):
                score += 2.0 * bonus
            elif _contains_pair(doc.content_tokens, first, second):
                score += bonus
        result[doc.node_id] = (score, frozenset(matched))
    return result


def _neighbours(edges: Iterable[Mapping[str, Any]]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for edge in edges:
        source = str(edge.get("source_node_id") or "")
        target = str(edge.get("target_node_id") or "")
        if source and target and source != target:
            result.setdefault(source, set()).add(target)
            result.setdefault(target, set()).add(source)
    return result


def _order_key(item: ScoredNode) -> tuple[float, str, str]:
    return (-item.score, item.title, item.node_id)


def rank_nodes(
    query_text: str,
    candidates: Sequence[Mapping[str, Any]],
    *,
    root_ids: Iterable[str],
    edges: Iterable[Mapping[str, Any]],
    excluded_types: frozenset[str] = DEFAULT_EXCLUDED_TYPES,
    type_weights: Mapping[str, float] = DEFAULT_TYPE_WEIGHTS,
) -> list[ScoredNode]:
    """Score every eligible candidate; roots and excluded types are never returned."""

    roots = {str(value) for value in root_ids}
    eligible: dict[str, Mapping[str, Any]] = {}
    for node in candidates:
        node_id = str(node.get("id") or "")
        if not node_id or node_id in roots or node_id in eligible:
            continue
        if str(node.get("semantic_type") or "") in excluded_types:
            continue
        eligible[node_id] = node
    tokens = tokenize(query_text)
    terms = list(dict.fromkeys(tokens))[:MAX_QUERY_TERMS]
    pairs = list(dict.fromkeys((tokens[i], tokens[i + 1]) for i in range(len(tokens) - 1) if tokens[i] != tokens[i + 1]))
    docs = [_doc(node_id, node) for node_id, node in eligible.items()]
    lexical = _bm25(dict.fromkeys(terms, 1.0), pairs, docs)
    scores = {node_id: lexical[node_id][0] for node_id in eligible}
    reasons = dict.fromkeys(eligible, "lexical")
    adjacency = _neighbours(edges)
    top_score = max(scores.values(), default=0.0)
    hits = sorted((item for item in scores.items() if item[1] > 0), key=lambda item: (-item[1], item[0]))
    for hit_id, hit_score in hits[:GRAPH_EXPANSION_HITS]:
        for neighbour in sorted(adjacency.get(hit_id, ())):
            expanded = GRAPH_ALPHA * hit_score
            if neighbour in scores and expanded > scores[neighbour]:
                scores[neighbour] = expanded
                reasons[neighbour] = "graph_local"
    anchor = GRAPH_ALPHA * max(top_score, 1.0)
    for root in sorted(roots):
        for neighbour in sorted(adjacency.get(root, ())):
            if neighbour in scores and anchor > scores[neighbour]:
                scores[neighbour] = anchor
                reasons[neighbour] = "graph_local"
    ranked: list[ScoredNode] = []
    for doc in docs:
        node = eligible[doc.node_id]
        semantic_type = str(node.get("semantic_type") or "")
        weight = float(type_weights.get(semantic_type, 1.0))
        ranked.append(
            ScoredNode(
                node_id=doc.node_id,
                title=str(node.get("title") or ""),
                semantic_type=semantic_type,
                score=round(scores[doc.node_id] * weight, 6),
                lexical=round(lexical[doc.node_id][0], 6),
                reason=reasons[doc.node_id],
                matched_terms=lexical[doc.node_id][1],
                tokens=frozenset(doc.tf),
                text_chars=len(str(node.get("content") or "")),
                node=node,
            )
        )
    ranked.sort(key=_order_key)
    return ranked


def _jaccard(first: frozenset[str], second: frozenset[str]) -> float:
    if not first and not second:
        return 0.0
    return len(first & second) / len(first | second)


def item_cost(item: ScoredNode, *, max_excerpt_chars: int) -> int:
    return len(item.title) + min(max(item.text_chars, len(item.title)), max_excerpt_chars)


def select_diverse(
    scored: Sequence[ScoredNode],
    *,
    budget_chars: int,
    max_items: int,
    max_excerpt_chars: int = 1_200,
) -> Selection:
    """MMR-style selection bounded by a character budget, with deterministic tie-breaks."""

    ordered = sorted((item for item in scored if item.score > 0), key=_order_key)
    dropped: list[tuple[ScoredNode, str]] = []
    if not ordered or budget_chars <= 0 or max_items <= 0:
        return Selection((), {}, tuple((item, "budget") for item in ordered), max(budget_chars, 0), 0)
    top = ordered[0].score
    pool = [item for item in ordered if item.score >= LOW_SCORE_FRACTION * top]
    dropped.extend((item, "low_score") for item in ordered if item.score < LOW_SCORE_FRACTION * top)
    universe = frozenset().union(*(item.matched_terms for item in pool))
    # Coverage counts discriminative query terms more than common ones (IDF over the ranked candidates).
    df = {term: sum(term in item.matched_terms for item in ordered) for term in universe}
    weight = {term: math.log1p(len(ordered) / df[term]) for term in universe if df[term]}
    universe_weight = sum(weight.values())
    selected: list[ScoredNode] = []
    reasons: dict[str, str] = {}
    covered: set[str] = set()
    remaining = budget_chars
    while pool and len(selected) < max_items and remaining > 0:
        best: tuple[float, float, str, str] | None = None
        best_item: ScoredNode | None = None
        for item in pool:
            similarity = max((_jaccard(item.tokens, chosen.tokens) for chosen in selected), default=0.0)
            new_terms = (sum(weight.get(term, 0.0) for term in item.matched_terms - covered) / universe_weight
                         if universe_weight else 0.0)
            value = MMR_LAMBDA * item.score / top - (1.0 - MMR_LAMBDA) * similarity + COVERAGE_BONUS * new_terms
            key = (-value, -item.score, item.title, item.node_id)
            if best is None or key < best:
                best, best_item = key, item
        if best_item is None:
            break
        pool.remove(best_item)
        similarity = max((_jaccard(best_item.tokens, chosen.tokens) for chosen in selected), default=0.0)
        if similarity >= NEAR_DUPLICATE_SIMILARITY:
            dropped.append((best_item, "near_duplicate"))
            continue
        cost = item_cost(best_item, max_excerpt_chars=max_excerpt_chars)
        if cost > remaining and remaining - len(best_item.title) < MIN_TRUNCATED_CHARS:
            dropped.append((best_item, "budget"))
            continue
        higher_waiting = any(_order_key(other) < _order_key(best_item) for other in pool) or any(
            reason == "near_duplicate" and _order_key(other) < _order_key(best_item) for other, reason in dropped
        )
        reasons[best_item.node_id] = "diversity" if higher_waiting else best_item.reason
        selected.append(best_item)
        covered |= best_item.matched_terms
        remaining -= min(cost, remaining)
    dropped.extend((item, "budget") for item in sorted(pool, key=_order_key))
    return Selection(tuple(selected), reasons, tuple(dropped), budget_chars, budget_chars - remaining)


def _summary(item: ScoredNode) -> dict[str, Any]:
    return {"node_id": item.node_id, "title": item.title[:240], "semantic_type": item.semantic_type,
            "score": item.score, "lexical": item.lexical}


def diagnostics(query_text: str, ranked: Sequence[ScoredNode], selection: Selection) -> dict[str, Any]:
    dropped = sorted(selection.dropped, key=lambda pair: _order_key(pair[0]))[:DIAGNOSTIC_DROPPED]
    return {
        "mode": RETRIEVAL_MODE,
        "semantic": SEMANTIC_STATUS,
        "query_terms": query_terms(query_text),
        "candidates_considered": len(ranked),
        "top_candidates": [_summary(item) for item in list(ranked)[:DIAGNOSTIC_TOP]],
        "selected": [
            _summary(item) | {"reason": selection.reasons.get(item.node_id, item.reason)}
            for item in selection.selected
        ],
        "dropped": [_summary(item) | {"reason": reason} for item, reason in dropped],
        "budget": {"chars": selection.budget_chars, "used_chars": selection.used_chars},
    }
