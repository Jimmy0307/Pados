"""FCM clamped concepts keep their EXACT initial value during computation (CR-14b N-02).

v2 quantized a clamped initial value to ``precision`` decimals before iterating, so with precision 1 a
concept clamped at 0.14 influenced its neighbours as 0.1. v3 keeps the decimal the user wrote for the
whole computation and rounds only the REPORTED representation (half-even on that decimal).
"""
from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any

import pytest
from _algo_harness import matrix
from pados.packs.algorithms import fcm
from pados.packs.algorithms.contracts import Dataset


def run(rows: list[list[float]], **parameters: Any) -> dict[str, Any]:
    result, _ = fcm.compute(Dataset.model_validate(matrix(("A", "B"), rows)), fcm.FcmParameters(**parameters))
    return result


def test_clamped_value_influences_neighbours_exactly() -> None:
    """w_AB = 1, A clamped at 0.14, B(0) = 0, sigmoid with lambda = 1.7, precision 1. By hand:
      B(1) = sig(1.7 * 0.14) = sig(0.238) = 1 / (1 + e^-0.238) = 0.5592 -> 0.6   (exact clamped value)
      (v2 used A = 0.1: sig(0.17) = 0.5424 -> 0.5)
      B(2) = 0.6 = B(1): FIXED_POINT after 2 iterations.
    A is reported half-even to one decimal: 0.1."""
    result = run([[0, 1], [0, 0]], initial_state={"A": 0.14, "B": 0}, clamped=("A",), steepness=1.7, precision=1)
    assert result["status"] == "FIXED_POINT" and result["iterations"] == 2
    assert result["trajectory"] == [[0.1, 0.0], [0.1, 0.6], [0.1, 0.6]]
    assert result["final_state"] == {"A": 0.1, "B": 0.6}


def test_clamped_value_is_the_exact_decimal_throughout() -> None:
    parameters = fcm.FcmParameters(initial_state={"A": 0.14, "B": 0.987654}, clamped=("A",), precision=1)
    weights = ((Decimal(0), Decimal(1)), (Decimal(0), Decimal(0)))
    with localcontext(fcm.decimal_context()):
        state = fcm.initial_state(("A", "B"), parameters, frozenset({0}))
        assert state == (Decimal("0.14"), Decimal("1.0"))  # clamped exact; free concept quantized
        for _ in range(3):
            state = fcm.step(state, weights, parameters, frozenset({0}))
            assert state[0] == Decimal("0.14")


def test_reported_clamped_value_is_rounded_half_even_on_the_decimal() -> None:
    """0.15 is the float 0.1499999...; the report rounds the decimal the user wrote, so it is 0.2 (half-even
    on 0.15), and 0.25 reports 0.2 (half-even). The computation still uses 0.15 / 0.25 exactly."""
    result = run([[0, 0], [0, 0]], initial_state={"A": 0.15, "B": 0.25}, clamped=("A", "B"), precision=1,
                 max_iterations=1)
    assert result["trajectory"][0] == [0.2, 0.2] and result["final_state"] == {"A": 0.2, "B": 0.2}


def test_unclamped_initial_state_is_still_quantized() -> None:
    result = run([[0, 0.5], [-0.5, 0]], initial_state={"A": 0.123456, "B": 0.5}, precision=3, max_iterations=1)
    assert result["trajectory"][0] == [0.123, 0.5]


def test_trajectory_is_not_converted_when_it_is_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-14c N-03: with record_trajectory=False only the final state is converted for reporting."""
    calls: list[int] = []
    real = fcm._reported

    def counting(value: Any, digits: int) -> float:
        calls.append(1)
        return real(value, digits)

    monkeypatch.setattr(fcm, "_reported", counting)
    common: dict[str, Any] = {"initial_state": {"A": 0.14, "B": 0}, "clamped": ("A",), "steepness": 1.7,
                              "precision": 1}
    with_trajectory = run([[0, 1], [0, 0]], record_trajectory=True, **common)
    full = len(calls)
    calls.clear()
    without = run([[0, 1], [0, 0]], record_trajectory=False, **common)
    assert without["trajectory"] is None and with_trajectory["final_state"] == without["final_state"]
    assert len(calls) == len(without["concepts"]) < full
