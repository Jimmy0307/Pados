"""DRSA/DOMLEM work is bounded by a deterministic operation budget and by max_rules INSIDE induction
(CR-14 H-02). Early abort is shown with the operation counter, never with timing."""
from __future__ import annotations

import random
from typing import Any

import pytest

from _algo_harness import students
from pados.packs.algorithms import drsa
from pados.packs.algorithms.contracts import DATASET_SCHEMA, AlgorithmError, Dataset


def _random_table(objects: int, criteria: int, classes: int, *, integer: bool, seed: int = 1) -> Dataset:
    rnd = random.Random(seed)  # structureless data: the expensive case for sequential covering
    domain = [f"k{i}" for i in range(classes)]
    columns: list[dict[str, Any]] = [{"name": f"c{i}", "type": "NUMERIC", "criterion": "GAIN"}
                                     for i in range(criteria)]
    columns.append({"name": "D", "type": "ORDINAL", "criterion": "GAIN", "domain": domain})
    rows = [[float(rnd.randint(0, 9)) if integer else rnd.random() for _ in range(criteria)] + [rnd.choice(domain)]
            for _ in range(objects)]
    return Dataset.model_validate({"schema": DATASET_SCHEMA, "columns": columns, "decision": "D",
                                   "object_ids": [f"o{i}" for i in range(objects)], "rows": rows})


def _run(dataset: Dataset, **parameters: Any) -> tuple[str, int]:
    """(outcome, operations used): outcome is 'ok' or the closed error code."""
    params = drsa.DrsaParameters(**parameters)
    budget = drsa.OperationBudget(params.operation_budget)
    try:
        drsa.compute(dataset, params, budget=budget)
    except AlgorithmError as exc:
        return exc.code, budget.used
    return "ok", budget.used


def test_budget_parameter_is_bounded_with_a_documented_default() -> None:
    assert drsa.DrsaParameters().operation_budget == drsa.DEFAULT_OPERATION_BUDGET == 20_000_000
    assert drsa.MAX_OPERATION_BUDGET == 100_000_000
    for bad in (0, drsa.MAX_OPERATION_BUDGET + 1):
        with pytest.raises(ValueError):
            drsa.DrsaParameters(operation_budget=bad)
    with pytest.raises(AlgorithmError, match="DRSA_OPERATION_BUDGET_INVALID"):
        drsa.OperationBudget(drsa.MAX_OPERATION_BUDGET + 1)
    with pytest.raises(AlgorithmError, match="DRSA_OPERATION_BUDGET_INVALID"):  # a meter must match the request
        drsa.compute(Dataset.model_validate(students()), drsa.DrsaParameters(), budget=drsa.OperationBudget(5))


def test_students_example_is_unchanged_and_reports_its_operations() -> None:
    dataset = Dataset.model_validate(students())
    result, metrics = drsa.compute(dataset, drsa.DrsaParameters())
    assert len(result["rules"]) == 6 and metrics["rule_count"] == 6.0
    again, metrics_again = drsa.compute(dataset, drsa.DrsaParameters())
    assert again == result and metrics_again["operations"] == metrics["operations"] > 0  # deterministic count
    # max_rules bounds rules HELD during induction: the 6-rule result needs exactly 6.
    assert _run(dataset, max_rules=6)[0] == "ok"
    assert _run(dataset, max_rules=5)[0] == "DRSA_RULE_LIMIT_EXCEEDED"


def test_max_rules_one_aborts_inside_induction() -> None:
    dataset = _random_table(300, 12, 4, integer=True)
    outcome, full = _run(dataset, max_rules=5000, operation_budget=drsa.MAX_OPERATION_BUDGET)
    assert outcome == "ok" and full > 500_000
    outcome, early = _run(dataset, max_rules=1, operation_budget=drsa.MAX_OPERATION_BUDGET)
    assert outcome == "DRSA_RULE_LIMIT_EXCEEDED"
    # Before: the limit was checked only after full induction + redundancy elimination of a union.
    # Now the second rule of the first union is refused, after cones, approximations and ONE rule. Since v3
    # the table, threshold masks and union memberships are metered as well, so ``early`` includes them.
    assert early * 25 < full


def test_tiny_budget_aborts_immediately_and_never_overspends() -> None:
    dataset = Dataset.model_validate(students())
    _, needed = _run(dataset)
    for limit in (1, 10, needed // 2, needed - 1):
        outcome, used = _run(dataset, operation_budget=limit)
        assert outcome == "DRSA_OPERATION_BUDGET_EXCEEDED" and used <= limit
    assert _run(dataset, operation_budget=needed) == ("ok", needed)


@pytest.mark.parametrize("integer", [True, False], ids=["integer", "continuous"])
def test_worst_case_at_the_declared_limits_terminates_within_the_budget(integer: bool) -> None:
    """1000 objects x 31 criteria x 32 classes (62 unions) of structureless data: whatever the data, the run
    stops at the budget (the counter is checked before every elementary step, so ``used <= limit``)."""
    dataset = _random_table(drsa.MAX_OBJECTS, drsa.MAX_CRITERIA - 1, drsa.MAX_CLASSES, integer=integer)
    limit = 400_000
    outcome, used = _run(dataset, max_rules=5000, operation_budget=limit)
    assert outcome == "DRSA_OPERATION_BUDGET_EXCEEDED"
    assert limit - drsa.MAX_OBJECTS <= used <= limit  # spent up to the limit, never past it
