"""DEMATEL versus hand computation, exact-inverse identities, and the contract-only MCDM methods.

Method: Gabus & Fontela (1972/1973); normalisation s = max(max row sum, max column sum) and
T = N (I - N)^-1 as in Tzeng, Chiang & Li (2007); threshold = mean of T by default.
"""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from _algo_harness import matrix, policy
from pados.capability.adapter import StageBindingError, StageRequest, bind_stage
from pados.capability.policy import PolicyCode
from pados.enterprise.classification import DataClassification
from pados.packs.algorithms import mcdm
from pados.packs.algorithms.adapters import AlgorithmAdapter
from pados.packs.algorithms.contracts import AlgorithmError, Dataset
from pados.packs.algorithms.registry import CHOQUET, DANP, ROV


def run(names: tuple[str, ...], rows: list[list[float]], **parameters: Any) -> dict[str, Any]:
    result, _ = mcdm.compute_dematel(Dataset.model_validate(matrix(names, rows)),
                                     mcdm.DematelParameters(**parameters))
    return result


def test_two_factor_example_by_hand() -> None:
    # A = [[0,3],[1,0]]: row sums 3,1; column sums 1,3 -> s = 3; N = [[0,1],[1/3,0]].
    # I - N = [[1,-1],[-1/3,1]], det = 2/3, inverse = [[3/2,3/2],[1/2,3/2]].
    # T = N (I-N)^-1 = [[1/2,3/2],[1/2,1/2]]; D = (2,1), R = (1,2); D+R = (3,3); D-R = (1,-1).
    # mean(T) = 3/4 -> only A->B (3/2) exceeds the threshold.
    result = run(("A", "B"), [[0, 3], [1, 0]])
    assert result["normalization"] == 3.0
    assert result["total_relation"] == [[0.5, 1.5], [0.5, 0.5]]
    assert result["threshold"] == 0.75
    assert [(s["factor"], s["dispatch"], s["receive"], s["prominence"], s["relation"], s["group"])
            for s in result["scores"]] == [("A", 2.0, 1.0, 3.0, 1.0, "CAUSE"), ("B", 1.0, 2.0, 3.0, -1.0, "EFFECT")]
    assert result["edges"] == [{"source": "A", "target": "B", "weight": 1.5}]


def test_exact_total_relation_satisfies_the_defining_identity() -> None:
    names = ("C1", "C2", "C3", "C4")
    direct = ((0.0, 3.0, 2.0, 1.0), (1.0, 0.0, 4.0, 0.0), (2.0, 1.0, 0.0, 3.0), (0.0, 2.0, 1.0, 0.0))
    exact = mcdm.dematel_exact(names, direct)
    n, t = exact.normalized, exact.total
    size = len(names)
    # T (I - N) = N, exactly, and T = N + N T (the matrix series identity).
    i_minus_n = [[Fraction(int(i == j)) - n[i][j] for j in range(size)] for i in range(size)]
    assert mcdm.multiply(t, i_minus_n) == n
    nt = mcdm.multiply(n, t)
    assert all(t[i][j] == n[i][j] + nt[i][j] for i in range(size) for j in range(size))
    assert exact.scale == 7  # max(row sums 6,5,6,3; column sums 3,6,7,4)
    # Truncated power series agrees with the exact inverse.
    power, series = n, [row[:] for row in n]
    for _ in range(80):
        power = mcdm.multiply(power, n)
        series = [[series[i][j] + power[i][j] for j in range(size)] for i in range(size)]
    assert all(abs(float(series[i][j] - t[i][j])) < 1e-9 for i in range(size) for j in range(size))


def test_explicit_threshold_and_cause_effect_groups() -> None:
    # s = 3 (row sums 3,3,0); A and B are symmetric, so D - R of A (and B) equals t_AC > 0; C only receives.
    result = run(("A", "B", "C"), [[0, 2, 1], [2, 0, 1], [0, 0, 0]], threshold_mode="VALUE", threshold_value=0.0)
    assert result["threshold"] == 0.0
    groups = {s["factor"]: s["group"] for s in result["scores"]}
    assert groups["A"] == groups["B"] == "CAUSE" and groups["C"] == "EFFECT"
    assert all(edge["weight"] > 0 for edge in result["edges"])


@pytest.mark.parametrize("rows,code", [
    ([[0, 1], [1, 0]], "DEMATEL_MATRIX_SINGULAR"),  # N = A, I - N = [[1,-1],[-1,1]] is singular
    ([[0, 2], [2, 0]], "DEMATEL_MATRIX_SINGULAR"),
    ([[0, 0], [0, 0]], "DEMATEL_ZERO_MATRIX"),
    ([[1, 2], [2, 0]], "DEMATEL_DIAGONAL_NOT_ZERO"),
    ([[0, -1], [2, 0]], "DEMATEL_MATRIX_VALUE_OUT_OF_RANGE"),
])
def test_singular_and_invalid_matrices_raise_explicit_errors(rows: list[list[float]], code: str) -> None:
    with pytest.raises(AlgorithmError) as caught:
        run(("A", "B"), rows)
    assert caught.value.code == code


def test_inverse_of_singular_matrix_is_refused() -> None:
    with pytest.raises(AlgorithmError, match="DEMATEL_MATRIX_SINGULAR"):
        mcdm.invert([[Fraction(1), Fraction(2)], [Fraction(2), Fraction(4)]])
    assert mcdm.invert([[Fraction(2), Fraction(0)], [Fraction(0), Fraction(4)]]) == [
        [Fraction(1, 2), Fraction(0)], [Fraction(0), Fraction(1, 4)]]


def test_shape_and_parameter_bounds() -> None:
    names = tuple(f"F{i}" for i in range(mcdm.MAX_FACTORS + 1))
    rows = [[0.0 if i == j else 1.0 for j in range(len(names))] for i in range(len(names))]
    with pytest.raises(AlgorithmError, match="DEMATEL_MATRIX_TOO_LARGE"):
        run(names, rows)
    with pytest.raises(AlgorithmError, match="DEMATEL_TOO_FEW_FACTORS"):
        run(("A",), [[0]])
    document = matrix(("A", "B"), [[0, 1], [2, 0]])
    document["columns"].append({"name": "D", "type": "ORDINAL", "criterion": "GAIN", "domain": ["x"]})
    document["rows"] = [[0, 1, "x"], [2, 0, "x"]]
    document["decision"] = "D"
    with pytest.raises(AlgorithmError, match="DEMATEL_MATRIX_HAS_DECISION"):
        mcdm.compute_dematel(Dataset.model_validate(document), mcdm.DematelParameters())
    with pytest.raises(ValueError, match="DEMATEL_THRESHOLD_INVALID"):
        mcdm.DematelParameters(threshold_mode="VALUE")
    with pytest.raises(ValueError, match="DEMATEL_THRESHOLD_INVALID"):
        mcdm.DematelParameters(threshold_value=0.5)


# --- contract-only methods -------------------------------------------------------------------------

@pytest.mark.parametrize("spec", [DANP, CHOQUET, ROV], ids=lambda s: s.algorithm_id)
def test_contract_only_methods_are_never_available(spec: Any, tmp_path: Path) -> None:
    adapter = AlgorithmAdapter(spec)
    assert adapter.available() is False and spec.compute is None
    ctx, decision, _ = policy(spec.capability, adapter)
    assert decision.code is PolicyCode.ADAPTER_UNAVAILABLE
    request = StageRequest("s1", spec.capability.capability_id, (), {}, DataClassification.CONFIDENTIAL)
    with pytest.raises(StageBindingError, match="NOT_ALLOWED"):
        bind_stage(spec.capability, adapter.descriptor, ctx, decision, request, permitted_inputs=frozenset())
    with pytest.raises(AlgorithmError, match="ALGORITHM_CONTRACT_ONLY"):
        spec.run(Dataset.model_validate(matrix(("A", "B"), [[0, 1], [1, 0]])), spec.parameters_model.model_validate(
            _example_parameters(spec.algorithm_id)))


def _example_parameters(algorithm_id: str) -> dict[str, Any]:
    return {"mcdm.danp": {"clusters": {"core": ["A", "B"]}},
            "mcdm.choquet": {"criteria": ["A", "B"], "capacity": {"A": 0.4, "B": 0.5, "A+B": 1.0}},
            "mcdm.rov": {"criteria_order": ["A", "B"]}}[algorithm_id]


def test_contract_only_schemas_validate_and_refuse_extras() -> None:
    assert mcdm.DanpResult(method="DANP", criteria=("A",), influential_weights={"A": 1.0}, limit_power=3)
    assert mcdm.ChoquetResult(method="CHOQUET", scores={"x": 0.5}, ranking=("x",))
    assert mcdm.RovResult(method="ROV", utility_ranges={"x": (0.1, 0.4)}, midpoint_ranking=("x",), dominated=())
    with pytest.raises(ValueError):
        mcdm.ChoquetParameters(criteria=("A",), capacity={"A": 1.0}, command="run")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        mcdm.DanpResult(method="DEMATEL", criteria=("A",), influential_weights={}, limit_power=1)
