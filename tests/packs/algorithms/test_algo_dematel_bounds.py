"""DEMATEL exact arithmetic is bounded (CR-14 M-01): inputs are decimals with at most 9 places (no
subnormal or extreme-exponent values, no silent rounding) and every intermediate Fraction is checked
against a numerator/denominator bit-length cap."""
from __future__ import annotations

import random
from fractions import Fraction
from typing import Any

import pytest

from _algo_harness import matrix
from pados.packs.algorithms import mcdm
from pados.packs.algorithms.contracts import AlgorithmError, Dataset


def run(rows: list[list[float]], **parameters: Any) -> dict[str, Any]:
    names = tuple(f"F{i}" for i in range(len(rows)))
    result, _ = mcdm.compute_dematel(Dataset.model_validate(matrix(names, rows)), mcdm.DematelParameters(**parameters))
    return result


@pytest.mark.parametrize("value,code", [
    (5e-324, "DEMATEL_VALUE_SUBNORMAL"),  # smallest subnormal: 10^-324 denominators before this fix
    (2.2250738585072014e-309, "DEMATEL_VALUE_SUBNORMAL"),
    (1e-300, "DEMATEL_VALUE_PRECISION_EXCEEDED"),  # normal but an extreme exponent
    (1e-10, "DEMATEL_VALUE_PRECISION_EXCEEDED"),
    (0.1234567891, "DEMATEL_VALUE_PRECISION_EXCEEDED"),  # 10 decimal places
    (123456.0000000001, "DEMATEL_VALUE_PRECISION_EXCEEDED"),
])
def test_values_beyond_the_declared_precision_are_refused(value: float, code: str) -> None:
    with pytest.raises(AlgorithmError) as caught:
        run([[0, value], [1, 0]])
    assert caught.value.code == code


def test_values_at_the_declared_precision_are_exact() -> None:
    # A = [[0, 1e-9], [2e-9, 0]]: s = 2e-9, N = [[0, 1/2], [1, 0]], det(I - N) = 1/2,
    # (I - N)^-1 = [[2, 1], [2, 2]], T = N (I - N)^-1 = [[1, 1], [2, 1]].
    result = run([[0, 1e-9], [2e-9, 0]])
    assert result["normalization"] == 2e-9
    assert result["total_relation"] == [[1.0, 1.0], [2.0, 1.0]]
    assert run([[0, 0.123456789], [1, 0]])["factors"] == ["F0", "F1"]  # 9 places accepted


def test_explicit_threshold_has_the_same_precision_bound() -> None:
    assert mcdm.DematelParameters(threshold_mode="VALUE", threshold_value=0.123456789)
    for bad in (0.1234567891, 5e-324):
        with pytest.raises(ValueError, match="DEMATEL_THRESHOLD_INVALID"):
            mcdm.DematelParameters(threshold_mode="VALUE", threshold_value=bad)


def test_intermediate_bit_lengths_are_capped() -> None:
    m = [[Fraction(3, 7), Fraction(1, 11)], [Fraction(2, 13), Fraction(5, 17)]]
    assert mcdm.invert(m) == mcdm.invert(m, max_bits=64)
    with pytest.raises(AlgorithmError, match="DEMATEL_ARITHMETIC_BUDGET_EXCEEDED"):
        mcdm.invert(m, max_bits=4)
    with pytest.raises(AlgorithmError, match="DEMATEL_ARITHMETIC_BUDGET_EXCEEDED"):
        mcdm.multiply(m, m, max_bits=6)
    huge = [[Fraction(1, 2**70), Fraction(0)], [Fraction(0), Fraction(1)]]
    with pytest.raises(AlgorithmError, match="DEMATEL_ARITHMETIC_BUDGET_EXCEEDED"):
        mcdm.invert(huge, max_bits=64)  # even the input row is checked before any elimination step


def test_worst_case_at_the_declared_limits_stays_far_below_the_cap() -> None:
    """32 factors, every off-diagonal entry a 9-place decimal close to 10^6: the exact result needs about
    1.7k bits, far below MAX_FRACTION_BITS (Hadamard: < 2k bits for any admissible input)."""
    rnd = random.Random(3)
    size = mcdm.MAX_FACTORS
    direct = tuple(tuple(0.0 if i == j else float(f"{rnd.randint(10**14, 10**15)}e-9") for j in range(size))
                   for i in range(size))
    exact = mcdm.dematel_exact(tuple(f"F{i}" for i in range(size)), direct)
    bits = max(max(v.numerator.bit_length(), v.denominator.bit_length()) for row in exact.total for v in row)
    assert 1000 < bits < mcdm.MAX_FRACTION_BITS // 4
