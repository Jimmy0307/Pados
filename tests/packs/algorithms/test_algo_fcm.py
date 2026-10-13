"""Fuzzy cognitive map inference versus hand-computed iterations.

Method sources: Kosko (1986) update A_i(t+1) = f(sum_{j!=i} A_j(t) w_ji); modified Kosko (Stylios &
Groumpos 2004) adds A_i(t); rescaled rule (Papageorgiou 2011) uses 2A-1. Row i, column j = w_ij.
"""
from __future__ import annotations

import math
from typing import Any

import pytest

from _algo_harness import matrix
from pados.packs.algorithms import fcm
from pados.packs.algorithms.contracts import AlgorithmError, Dataset


def sig(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def run(rows: list[list[float]], names: tuple[str, ...] = ("A", "B"), **parameters: Any) -> dict[str, Any]:
    result, _ = fcm.compute(Dataset.model_validate(matrix(names, rows)), fcm.FcmParameters(**parameters))
    return result


def test_trivalent_limit_cycle_of_period_four() -> None:
    # w_AB = 1, w_BA = -1; (1,0) -> (0,1) -> (-1,0) -> (0,-1) -> (1,0): back to the start after 4 steps.
    result = run([[0, 1], [-1, 0]], initial_state={"A": 1, "B": 0}, transfer="TRIVALENT")
    assert result["status"] == "LIMIT_CYCLE" and result["period"] == 4 and result["iterations"] == 4
    assert result["trajectory"] == [[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0], [0.0, -1.0], [1.0, 0.0]]


def test_sigmoid_fixed_point_by_hand() -> None:
    # A has no inputs: A = sig(0) = 0.5 from t=1. B = sig(0.5 * A) -> sig(0.25) from t=2; t=3 repeats.
    result = run([[0, 0.5], [0, 0]], initial_state={"A": 1, "B": 1})
    assert result["status"] == "FIXED_POINT" and result["period"] == 1 and result["iterations"] == 3
    assert result["final_state"] == {"A": 0.5, "B": round(sig(0.25), 10)}
    assert result["trajectory"][1] == [0.5, round(sig(0.5), 10)]


def test_modified_kosko_and_rescaled_single_step() -> None:
    modified = run([[0, 0.5], [0, 0]], initial_state={"A": 1, "B": 0}, update_rule="MODIFIED_KOSKO",
                   max_iterations=1)
    assert modified["status"] == "MAX_ITERATIONS" and modified["period"] is None
    assert modified["final_state"] == {"A": round(sig(1.0), 10), "B": round(sig(0.5), 10)}
    rescaled = run([[0, 0.5], [0, 0]], initial_state={"A": 1, "B": 0}, update_rule="RESCALED", max_iterations=1)
    # A: (2*1-1) + (2*0-1)*0 = 1 ; B: (2*0-1) + (2*1-1)*0.5 = -0.5
    assert rescaled["final_state"] == {"A": round(sig(1.0), 10), "B": round(sig(-0.5), 10)}


def test_tanh_steepness_and_clamping() -> None:
    result = run([[0, 1], [0, 0]], initial_state={"A": 0.5, "B": 0}, transfer="TANH", steepness=2.0,
                 clamped=("A",))
    assert result["status"] == "FIXED_POINT"
    assert result["final_state"] == {"A": 0.5, "B": round(math.tanh(2.0 * 0.5), 10)}


def test_bivalent_converges_and_trajectory_is_optional() -> None:
    result = run([[0, 1], [1, 0]], initial_state={"A": 1, "B": 0}, transfer="BIVALENT", record_trajectory=False)
    assert result["status"] == "LIMIT_CYCLE" and result["period"] == 2 and result["trajectory"] is None
    assert run([[0, 1], [1, 0]], initial_state={"A": 1, "B": 1}, transfer="BIVALENT")["status"] == "FIXED_POINT"


@pytest.mark.parametrize("rows,parameters,code", [
    ([[0.5, 1], [0, 0]], {"initial_state": {"A": 0, "B": 0}}, "FCM_SELF_LOOP_NOT_ALLOWED"),
    ([[0, 1.5], [0, 0]], {"initial_state": {"A": 0, "B": 0}}, "FCM_MATRIX_VALUE_OUT_OF_RANGE"),
    ([[0, 1], [0, 0]], {"initial_state": {"A": 0}}, "FCM_INITIAL_STATE_CONCEPTS_MISMATCH"),
    ([[0, 1], [0, 0]], {"initial_state": {"A": -0.5, "B": 0}}, "FCM_INITIAL_STATE_OUT_OF_RANGE"),
    ([[0, 1], [0, 0]], {"initial_state": {"A": 0, "B": 0}, "clamped": ("Z",)}, "FCM_CLAMPED_UNKNOWN"),
])
def test_invalid_maps_fail_closed(rows: list[list[float]], parameters: dict[str, Any], code: str) -> None:
    with pytest.raises(AlgorithmError) as caught:
        run(rows, **parameters)
    assert caught.value.code == code


def test_non_square_and_missing_values_fail_closed() -> None:
    document = matrix(("A", "B"), [[0, 1], [0, 0]])
    document["object_ids"] = ["A", "C"]
    with pytest.raises(AlgorithmError, match="FCM_MATRIX_NOT_SQUARE"):
        fcm.compute(Dataset.model_validate(document), fcm.FcmParameters(initial_state={"A": 0, "B": 0}))
    document = matrix(("A", "B"), [[0, None], [0, 0]])  # type: ignore[list-item]
    document["missing"] = "DROP_OBJECT"
    with pytest.raises(AlgorithmError, match="FCM_MATRIX_MISSING_VALUE"):
        fcm.compute(Dataset.model_validate(document), fcm.FcmParameters(initial_state={"A": 0, "B": 0}))


@pytest.mark.parametrize("parameters", [
    {"initial_state": {"A": 0, "B": 0}, "max_iterations": 501},
    {"initial_state": {"A": 0, "B": 0}, "steepness": 0},
    {"initial_state": {"A": 0, "B": 0}, "epsilon": 0},
    {"initial_state": {"A": float("nan"), "B": 0}},
    {"initial_state": {"A": 0, "B": 0}, "unexpected": 1},
])
def test_parameter_schema_is_bounded(parameters: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        fcm.FcmParameters(**parameters)


def test_numeric_stability_with_extreme_steepness() -> None:
    names = tuple(f"C{i}" for i in range(fcm.MAX_CONCEPTS))
    rows = [[0.0 if i == j else (-1.0 if (i + j) % 2 else 1.0) for j in range(len(names))] for i in range(len(names))]
    result = run(rows, names, initial_state={n: 1.0 for n in names}, steepness=100.0, max_iterations=50)
    assert all(0.0 <= v <= 1.0 and math.isfinite(v) for v in result["final_state"].values())
