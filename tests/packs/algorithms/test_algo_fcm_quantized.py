"""FCM trajectories are decided on quantized state, independent of the platform libm (CR-14 M-04).

Every step is computed in standard-library decimal arithmetic (correctly rounded exp/division) and the new
state is rounded half-even to ``precision`` decimals BEFORE the epsilon test and cycle detection.
"""
from __future__ import annotations

import math
from decimal import Decimal, localcontext
from typing import Any

import pytest

from _algo_harness import matrix
from pados.packs.algorithms import fcm
from pados.packs.algorithms.contracts import Dataset


def run(rows: list[list[float]], names: tuple[str, ...] = ("A", "B"), **parameters: Any) -> dict[str, Any]:
    result, _ = fcm.compute(Dataset.model_validate(matrix(names, rows)), fcm.FcmParameters(**parameters))
    return result


def test_convergence_is_decided_on_quantized_state() -> None:
    # w_AB = 0.5, w_BA = -0.5, sigmoid, A(0) = B(0) = 1, precision 2, epsilon 1e-12 (finer than the grid).
    # By hand (sig(x) = 1 / (1 + e^-x), each value rounded half-even to 2 places after every step):
    #   t1: A = sig(-0.5)  = 0.3775 -> 0.38   B = sig(0.5)  = 0.6225 -> 0.62
    #   t2: A = sig(-0.31) = 0.4231 -> 0.42   B = sig(0.19) = 0.5474 -> 0.55
    #   t3: A = sig(-0.275)= 0.4317 -> 0.43   B = sig(0.21) = 0.5523 -> 0.55
    #   t4: A = sig(-0.275)-> 0.43            B = sig(0.215)= 0.5535 -> 0.55   == t3: FIXED_POINT
    # Unquantized iteration needs 14 steps to get within 1e-12; the trajectory is now decided on the grid.
    result = run([[0, 0.5], [-0.5, 0]], initial_state={"A": 1, "B": 1}, precision=2, epsilon=1e-12)
    assert result["status"] == "FIXED_POINT" and result["iterations"] == 4
    assert result["trajectory"] == [[1.0, 1.0], [0.38, 0.62], [0.42, 0.55], [0.43, 0.55], [0.43, 0.55]]


def test_initial_state_is_quantized_too() -> None:
    result = run([[0, 0.5], [-0.5, 0]], initial_state={"A": 0.123456, "B": 0.5}, precision=3, max_iterations=1)
    assert result["trajectory"][0] == [0.123, 0.5]
    tie = run([[0, 0], [0, 0]], initial_state={"A": 0.125, "B": 0.375}, transfer="SIGMOID", precision=2,
              clamped=("A", "B"), max_iterations=1)
    assert tie["trajectory"][0] == [0.12, 0.38]  # half-even on the decimal the user wrote


def test_step_returns_quantized_decimals() -> None:
    parameters = fcm.FcmParameters(initial_state={"A": 1, "B": 1}, precision=4)
    weights = ((Decimal(0), Decimal("0.5")), (Decimal("-0.5"), Decimal(0)))
    with localcontext(fcm.decimal_context()):
        state = fcm.step((Decimal(1), Decimal(1)), weights, parameters, frozenset())
    assert all(isinstance(v, Decimal) and v == v.quantize(Decimal("0.0001")) for v in state)
    assert state == (Decimal("0.3775"), Decimal("0.6225"))


def test_platform_libm_is_never_used(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = run([[0, 1], [0, 0]], initial_state={"A": 0.5, "B": 0}, transfer="TANH", steepness=2.0)
    sigmoid = run([[0, 0.5], [-0.5, 0]], initial_state={"A": 1, "B": 1}, precision=2)

    def refuse(*_: object) -> float:
        raise AssertionError("libm used")

    for name in ("exp", "tanh", "fsum"):
        monkeypatch.setattr(fcm.math, name, refuse)
    assert run([[0, 1], [0, 0]], initial_state={"A": 0.5, "B": 0}, transfer="TANH", steepness=2.0) == expected
    assert run([[0, 0.5], [-0.5, 0]], initial_state={"A": 1, "B": 1}, precision=2) == sigmoid


def test_extreme_arguments_stay_finite_and_on_the_grid() -> None:
    names = tuple(f"C{i}" for i in range(8))
    rows = [[0.0 if i == j else 1.0 for j in range(8)] for i in range(8)]
    for transfer in ("SIGMOID", "TANH"):
        result = run(rows, names, initial_state={n: -1.0 if transfer == "TANH" else 0.0 for n in names},
                     transfer=transfer, steepness=100.0, precision=15, max_iterations=5)
        values = [v for state in result["trajectory"] for v in state]
        assert all(math.isfinite(v) and round(v, 15) == v for v in values)
