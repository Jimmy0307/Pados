"""DEMATEL arithmetic is checked per operation and metered by one shared budget (CR-14b M-01).

Every product, quotient, difference and partial sum is one budget step, and its bit length is checked
right after that step, so an oversized intermediate is caught even when the row it belongs to (or the
final dot product) would have been small again.
"""
from __future__ import annotations

import random
from fractions import Fraction
from typing import Any

import pytest

from _algo_harness import matrix
from pados.packs.algorithms import mcdm
from pados.packs.algorithms.contracts import AlgorithmError, Dataset

P, Q = 1_000_003, 999_983  # primes near 2^20: 1/(P*Q) needs about 40 bits


def run(rows: list[list[float]], **parameters: Any) -> tuple[dict[str, Any], dict[str, float]]:
    names = tuple(f"F{i}" for i in range(len(rows)))
    return mcdm.compute_dematel(Dataset.model_validate(matrix(names, rows)), mcdm.DematelParameters(**parameters))


def test_invert_trips_on_one_product_before_the_row_completes() -> None:
    """M = [[1, b], [f, a]] with f = a = 1/P and b = (Q - P)/Q. Every entry needs about 20 bits, and so does
    every entry of every eliminated row: row 1 becomes [0, 1/Q, -1/P, 1]. Only the single product
    f * b = (Q - P)/(P Q) needs about 40 bits, so a 32-bit cap is exceeded only by that step.

    Steps: 8 input checks, 4 pivot-row divisions, then row 1 = (sub, mul) per entry: mul(f, 1), sub -> 14,
    mul(f, b) is step 15 and fails; the row would have needed 8 elimination steps (up to step 20)."""
    m = [[Fraction(1), Fraction(Q - P, Q)], [Fraction(1, P), Fraction(1, P)]]
    exact = mcdm.invert(m, max_bits=64)
    assert all(max(v.numerator.bit_length(), v.denominator.bit_length()) <= 32 for row in exact for v in row)
    budget = mcdm.ArithmeticBudget()
    with pytest.raises(AlgorithmError) as caught:
        mcdm.invert(m, max_bits=32, budget=budget)
    assert caught.value.code == "DEMATEL_ARITHMETIC_BUDGET_EXCEEDED"
    assert budget.used == 15 < 20


def test_multiply_trips_on_a_product_whose_dot_product_cancels() -> None:
    """[1/P, -1/P] . [1/Q, 1/Q] = 0, but each product 1/(P Q) needs about 40 bits."""
    left = [[Fraction(1, P), Fraction(-1, P)]]
    right = [[Fraction(1, Q)], [Fraction(1, Q)]]
    assert mcdm.multiply(left, right, max_bits=64) == [[Fraction(0)]]
    budget = mcdm.ArithmeticBudget()
    with pytest.raises(AlgorithmError, match="DEMATEL_ARITHMETIC_BUDGET_EXCEEDED"):
        mcdm.multiply(left, right, max_bits=32, budget=budget)
    assert budget.used == 1  # the very first product


def test_partial_sums_are_checked() -> None:
    """[1/P, 1/Q, -1/Q] . [1, 1, 1]: products fit, the partial sum 1/P + 1/Q needs about 40 bits."""
    budget = mcdm.ArithmeticBudget()
    with pytest.raises(AlgorithmError, match="DEMATEL_ARITHMETIC_BUDGET_EXCEEDED"):
        mcdm.multiply([[Fraction(1, P), Fraction(1, Q), Fraction(-1, Q)]], [[Fraction(1)]] * 3, max_bits=32,
                      budget=budget)
    assert budget.used == 4  # mul, add (0 + 1/P), mul, add (1/P + 1/Q) fails


def test_operation_budget_is_shared_bounded_and_enforced() -> None:
    assert mcdm.DematelParameters().operation_budget == mcdm.DEFAULT_OPERATION_BUDGET == 1_000_000
    assert mcdm.MAX_OPERATION_BUDGET == 10_000_000
    for bad in (0, mcdm.MAX_OPERATION_BUDGET + 1):
        with pytest.raises(ValueError):
            mcdm.DematelParameters(operation_budget=bad)
    with pytest.raises(AlgorithmError, match="DEMATEL_OPERATION_BUDGET_INVALID"):
        mcdm.ArithmeticBudget(mcdm.MAX_OPERATION_BUDGET + 1)
    rows = [[0, 3, 2], [1, 0, 1], [2, 4, 0]]
    _, metrics = run(rows)
    needed = int(metrics["operations"])
    assert needed > 0 and run(rows)[1]["operations"] == needed  # deterministic count
    assert run(rows, operation_budget=needed)[1]["operations"] == needed
    for limit in (1, needed // 2, needed - 1):
        with pytest.raises(AlgorithmError, match="DEMATEL_OPERATION_BUDGET_EXCEEDED"):
            run(rows, operation_budget=limit)


def test_one_budget_spans_normalisation_inverse_product_and_scores() -> None:
    """3 factors, explicit count: 9 input checks, 2 x 3 x 3 = 18 row/column partial sums, 9 divisions,
    9 subtractions (I - N), the inverse (18 input checks + per column 6 divisions and 2 rows x 6 x 2
    steps = 3 x 30 = 90, every off-pivot entry being non-zero here), the product (3 x 3 x 3 x 2 = 54),
    D and R (2 x 9 = 18), the mean threshold (3 + 1) and D - R, D + R (2 x 3): 9 + 18 + 9 + 9 + 18 + 90 +
    54 + 18 + 4 + 6 = 235."""
    _, metrics = run([[0, 3, 2], [1, 0, 1], [2, 4, 0]])
    assert metrics["operations"] == 235.0


def test_hand_computed_examples_are_unchanged() -> None:
    # A = [[0, 1e-9], [2e-9, 0]]: N = [[0, 1/2], [1, 0]], (I - N)^-1 = [[2, 1], [2, 2]], T = [[1, 1], [2, 1]].
    result, _ = run([[0, 1e-9], [2e-9, 0]])
    assert result["normalization"] == 2e-9
    assert result["total_relation"] == [[1.0, 1.0], [2.0, 1.0]]
    assert [s["prominence"] for s in result["scores"]] == [5.0, 5.0]
    assert [s["relation"] for s in result["scores"]] == [-1.0, 1.0]


def test_worst_case_at_the_declared_limits_fits_the_default_budget() -> None:
    rnd = random.Random(3)
    size = mcdm.MAX_FACTORS
    rows = [[0.0 if i == j else float(f"{rnd.randint(10**14, 10**15)}e-9") for j in range(size)]
            for i in range(size)]
    _, metrics = run(rows)
    assert 100_000 < metrics["operations"] < mcdm.DEFAULT_OPERATION_BUDGET // 2


class _Exploding(Fraction):
    """A rational whose arithmetic must never run once the budget is exhausted."""

    def __mul__(self, other: Any) -> Fraction:  # type: ignore[override]
        raise AssertionError("OPERATION_PERFORMED_AFTER_BUDGET")

    __rmul__ = __add__ = __radd__ = __sub__ = __rsub__ = __truediv__ = __rtruediv__ = __mul__  # type: ignore[assignment]


def test_budget_is_reserved_before_the_operation_runs() -> None:
    """CR-14c M-01: an exhausted budget refuses the NEXT operation before it is computed."""
    budget = mcdm.ArithmeticBudget(1)
    arithmetic = mcdm._Arithmetic(budget, mcdm.MAX_FRACTION_BITS)
    assert arithmetic.mul(Fraction(1, 2), Fraction(1, 3)) == Fraction(1, 6)
    for operation in (arithmetic.mul, arithmetic.add, arithmetic.sub, arithmetic.div):
        with pytest.raises(AlgorithmError, match="^DEMATEL_OPERATION_BUDGET_EXCEEDED$"):
            operation(_Exploding(1, 2), _Exploding(1, 3))
    assert budget.used == 1
