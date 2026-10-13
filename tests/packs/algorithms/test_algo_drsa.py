"""DRSA approximations and DOMLEM rules versus a hand-computed example.

Source of the method: Greco, Matarazzo & Slowinski, EJOR 129 (2001) (dominance cones, approximations,
quality of approximation); Greco, Matarazzo, Slowinski & Stefanowski, RSCTC 2000 (DOMLEM).
Data: students example (see ``_algo_harness.students``). Hand derivation, with b=0, m=1, g=2:

    S1 (2,1,0) bad   S2 (1,1,0) med   S3 (1,1,1) med   S4 (2,2,1) good
    S5 (2,1,2) good  S6 (2,2,2) good  S7 (0,0,0) bad   S8 (0,0,1) bad

S1 dominates S2 but is in a worse class, so S1/S2 are the inconsistent pair.
  D+(S1)={1,4,5,6}  D+(S2)={1..6}  D+(S3)={3,4,5,6}  D+(S8)={3,4,5,6,8}
  D-(S1)={1,2,7}    D-(S2)={2,7}   D-(S3)={2,3,7,8}  D-(S8)={7,8}
  Cl_good>=  lower {4,5,6}        upper {4,5,6}
  Cl_med>=   lower {3,4,5,6}      upper {1..6}            boundary {1,2}
  Cl_bad<=   lower {7,8}          upper {1,2,7,8}         boundary {1,2}
  Cl_med<=   lower {1,2,3,7,8}    upper {1,2,3,7,8}
  quality = |U - {1,2}| / |U| = 6/8 = 0.75
DOMLEM (consistency, then coverage of uncovered, then criterion order M<P<L):
  >=good: P>=g covers {4,6} (ties L>=g at 1.0/2, P first); then L>=g covers {5,6}.
  >=med:  P>=g, L>=g, then L>=m (4/5) + M>=m (4/4, ties P>=m) -> {M>=m, L>=m} covers {3,4,5,6};
          the first two are then redundant (the third covers B) and are dropped.
  <=bad:  M<=b covers {7,8} (ties P<=b).
  <=med:  M<=m (4/4, coverage 4) covers {2,3,7,8}; L<=b covers {1,2,7}.
"""
from __future__ import annotations

from typing import Any

import pytest

from _algo_harness import LEVELS, students
from pados.packs.algorithms import drsa
from pados.packs.algorithms.contracts import AlgorithmError, Dataset

S = [f"S{i}" for i in range(1, 9)]


def run(document: dict[str, Any], **parameters: Any) -> dict[str, Any]:
    result, _ = drsa.compute(Dataset.model_validate(document), drsa.DrsaParameters(**parameters))
    return result


def rules(result: dict[str, Any]) -> list[tuple[tuple[tuple[str, str, Any], ...], str, str, tuple[str, ...]]]:
    return [(tuple((c["attribute"], c["operator"], c["value"]) for c in r["conditions"]),
             r["conclusion"]["union"], r["conclusion"]["decision_class"], tuple(r["covered_objects"]))
            for r in result["rules"]]


def test_approximations_and_quality_match_hand_computation() -> None:
    result = run(students())
    table = {(a["union"], a["decision_class"]): a for a in result["approximations"]}
    assert table["AT_LEAST", "good"]["lower"] == ["S4", "S5", "S6"] == table["AT_LEAST", "good"]["upper"]
    assert table["AT_LEAST", "medium"]["lower"] == ["S3", "S4", "S5", "S6"]
    assert table["AT_LEAST", "medium"]["upper"] == ["S1", "S2", "S3", "S4", "S5", "S6"]
    assert table["AT_LEAST", "medium"]["boundary"] == ["S1", "S2"]
    assert table["AT_MOST", "bad"]["lower"] == ["S7", "S8"]
    assert table["AT_MOST", "bad"]["upper"] == ["S1", "S2", "S7", "S8"]
    assert table["AT_MOST", "medium"]["lower"] == ["S1", "S2", "S3", "S7", "S8"]
    assert table["AT_MOST", "medium"]["boundary"] == []
    assert table["AT_LEAST", "medium"]["accuracy"] == pytest.approx(4 / 6, abs=1e-10)
    assert table["AT_MOST", "bad"]["accuracy"] == 0.5
    assert result["quality_of_approximation"] == 0.75
    assert result["inconsistent_objects"] == ["S1", "S2"]
    assert result["classes"] == LEVELS and result["criteria"] == ["M", "P", "L"]


def test_domlem_rules_match_hand_computation() -> None:
    assert rules(run(students())) == [
        ((("P", ">=", "good"),), "AT_LEAST", "good", ("S4", "S6")),
        ((("L", ">=", "good"),), "AT_LEAST", "good", ("S5", "S6")),
        ((("M", ">=", "medium"), ("L", ">=", "medium")), "AT_LEAST", "medium", ("S3", "S4", "S5", "S6")),
        ((("M", "<=", "bad"),), "AT_MOST", "bad", ("S7", "S8")),
        ((("M", "<=", "medium"),), "AT_MOST", "medium", ("S2", "S3", "S7", "S8")),
        ((("L", "<=", "bad"),), "AT_MOST", "medium", ("S1", "S2", "S7")),
    ]


def test_every_rule_is_certain_and_rules_cover_each_lower_approximation() -> None:
    result = run(students())
    lowers = {(a["union"], a["decision_class"]): set(a["lower"]) for a in result["approximations"]}
    covered: dict[tuple[str, str], set[str]] = {key: set() for key in lowers}
    for rule in result["rules"]:
        key = (rule["conclusion"]["union"], rule["conclusion"]["decision_class"])
        assert set(rule["covered_objects"]) <= lowers[key]  # certain: only lower-approximation objects
        assert rule["support"] == len(rule["covered_objects"]) and rule["rule_type"] == "CERTAIN"
        covered[key] |= set(rule["covered_objects"])
    # Each lower approximation is covered by its own rules or by minimal rules of a stronger union.
    assert covered["AT_LEAST", "good"] | covered["AT_LEAST", "medium"] >= lowers["AT_LEAST", "medium"]
    assert covered["AT_MOST", "bad"] | covered["AT_MOST", "medium"] >= lowers["AT_MOST", "medium"]


def test_cost_criterion_flips_operators_but_not_semantics() -> None:
    document = students()
    document["columns"][0] = {"name": "M", "type": "ORDINAL", "criterion": "COST", "domain": LEVELS[::-1]}
    flipped = rules(run(document))
    original = rules(run(students()))
    assert [r[1:] for r in flipped] == [r[1:] for r in original]
    assert flipped[2][0] == (("M", "<=", "medium"), ("L", ">=", "medium"))
    assert flipped[3][0] == (("M", ">=", "bad"),)


def test_numeric_criteria_give_the_same_structure() -> None:
    document = students()
    scale = {"bad": 0.0, "medium": 5.0, "good": 10.0}
    for column in document["columns"][:3]:
        column.update({"type": "NUMERIC", "domain": [], "minimum": 0, "maximum": 10})
    document["rows"] = [[scale[v] for v in row[:3]] + [row[3]] for row in document["rows"]]
    numeric = rules(run(document))
    assert numeric[2][0] == (("M", ">=", 5.0), ("L", ">=", 5.0))
    assert [r[1:] for r in numeric] == [r[1:] for r in rules(run(students()))]


def test_consistent_data_has_quality_one_and_regular_attributes_use_equality() -> None:
    document = students()
    document["columns"].insert(0, {"name": "Group", "type": "NOMINAL", "criterion": "NONE", "domain": ["x", "y"]})
    for index, row in enumerate(document["rows"]):
        row.insert(0, "x" if index in (0, 6, 7) else "y")  # S1 now differs from S2 on a regular attribute
    result = run(document)
    assert result["quality_of_approximation"] == 1.0 and result["inconsistent_objects"] == []
    assert all(c["operator"] == "=" for r in result["rules"] for c in r["conditions"] if c["attribute"] == "Group")


def test_missing_values_policy() -> None:
    document = students()
    document["rows"][1][0] = None
    with pytest.raises(Exception, match="DATASET_MISSING_VALUE"):
        Dataset.model_validate(document)
    document["missing"] = "DROP_OBJECT"
    result = run(document)
    assert result["dropped_objects"] == 1 and result["objects"] == 7
    assert result["quality_of_approximation"] == 1.0  # S2 removed: no inconsistency left


@pytest.mark.parametrize("mutate,parameters,code", [
    (lambda d: d.update(decision=None), {}, "DRSA_DECISION_REQUIRED"),
    (lambda d: d["columns"][3].update(criterion="NONE"), {}, "DRSA_DECISION_NOT_ORDERED"),
    (lambda d: None, {"criteria": ("M", "Overall")}, "DRSA_CRITERIA_INVALID"),
    (lambda d: None, {"criteria": ("Q",)}, "DATASET_COLUMN_UNKNOWN"),
    (lambda d: None, {"max_rules": 2}, "DRSA_RULE_LIMIT_EXCEEDED"),
])
def test_invalid_inputs_fail_closed_with_codes(mutate: Any, parameters: dict[str, Any], code: str) -> None:
    document = students()
    mutate(document)
    with pytest.raises(AlgorithmError) as caught:
        run(document, **parameters)
    assert caught.value.code == code


def test_object_bound() -> None:
    columns = [{"name": "A", "type": "NUMERIC", "criterion": "GAIN"},
               {"name": "D", "type": "ORDINAL", "criterion": "GAIN", "domain": ["lo", "hi"]}]
    size = drsa.MAX_OBJECTS + 1
    document = {"schema": "pados.algorithms.dataset.v1", "columns": columns, "decision": "D",
                "object_ids": [f"o{i}" for i in range(size)], "rows": [[float(i), "lo"] for i in range(size)]}
    with pytest.raises(AlgorithmError, match="DRSA_TOO_MANY_OBJECTS"):
        run(document)


def test_subset_of_criteria_changes_dominance() -> None:
    result = run(students(), criteria=("M",))
    assert result["criteria"] == ["M"]
    assert result["quality_of_approximation"] < 0.75  # coarser dominance, more inconsistency
    assert all(c["attribute"] == "M" for r in result["rules"] for c in r["conditions"])
