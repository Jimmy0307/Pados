"""DRSA work is metered from the first step, generalization is charged per condition, and an optional
wall-clock deadline can only ABORT a run (CR-14b H-02).

The deadline is shown with a fake monotonic clock substituted into ``meter`` (no sleeps, no threads).
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from _algo_harness import matrix, put_json, run_bound, run_stage, stored_objects, students
from pados.capability.adapter import CapabilityAdapterError
from pados.capability.artifacts import ObjectStoreArtifacts
from pados.packs.algorithms import AlgorithmAdapter, ExpectedStage, drsa, fcm, mcdm, meter, verify_replay
from pados.packs.algorithms.contracts import AlgorithmError, Completion, Dataset, ReproducibilityRecord
from pados.packs.algorithms.registry import DEMATEL, DRSA, FCM


class _Clock:
    """Fake ``time.monotonic``: advances by ``tick`` seconds per reading."""

    def __init__(self, tick: float) -> None:
        self.now = 1000.0
        self.tick = tick
        self.readings = 0

    def __call__(self) -> float:
        self.readings += 1
        self.now += self.tick
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Clock]:
    fake = _Clock(tick=0.0)
    monkeypatch.setattr(meter, "monotonic", fake)
    yield fake


class _ConesReached(Exception):
    pass


def test_table_construction_is_metered_before_induction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Students: 8 rows x 4 columns read (32), three ordinal criteria with a 3-value domain looked up per
    row (3 x 8 x 3 = 72) and the decision class looked up per row (8 x 3 = 24): 128 steps before the
    dominance cones start."""
    seen: list[int] = []

    def stop(table: Any, budget: drsa.OperationBudget) -> Any:
        seen.append(budget.used)
        raise _ConesReached

    monkeypatch.setattr(drsa, "_cones", stop)
    params = drsa.DrsaParameters()
    budget = drsa.OperationBudget(params.operation_budget)
    with pytest.raises(_ConesReached):
        drsa.compute(Dataset.model_validate(students()), params, budget=budget)
    assert seen == [128]


def test_a_budget_smaller_than_the_table_work_aborts_before_any_cone(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(*_: Any) -> Any:
        raise _ConesReached

    monkeypatch.setattr(drsa, "_cones", unreachable)
    for limit in (1, 31, 127):
        params = drsa.DrsaParameters(operation_budget=limit)
        budget = drsa.OperationBudget(limit)
        with pytest.raises(AlgorithmError) as caught:
            drsa.compute(Dataset.model_validate(students()), params, budget=budget)
        assert caught.value.code == "DRSA_OPERATION_BUDGET_EXCEEDED" and budget.used <= limit


def test_threshold_masks_and_union_memberships_are_metered() -> None:
    params = drsa.DrsaParameters()
    budget = drsa.OperationBudget(params.operation_budget)
    table, _ = drsa._table(Dataset.model_validate(students()), params, budget)
    before = budget.used
    table.mask(0, ">=", 2.0)  # build: one step per object (8) + one per distinct value (3)
    assert budget.used - before == 8 + 3
    table.mask(0, ">=", 1.0)  # cached: free
    assert budget.used - before == 11
    drsa._members(table, 1, True)
    assert budget.used - before == 11 + 8


def test_generalization_is_charged_per_condition_comparison() -> None:
    params = drsa.DrsaParameters()
    budget = drsa.OperationBudget(params.operation_budget)
    table, _ = drsa._table(Dataset.model_validate(students()), params, budget)
    specific = drsa._Candidate({0: 2.0, 1: 2.0, 2: 1.0}, 0)
    cases = [
        (drsa._Candidate({0: 1.0, 1: 1.0, 2: 1.0}, 0), True, 3),  # all three conditions compared
        (drsa._Candidate({0: 1.0, 1: 2.0}, 0), True, 2),
        (drsa._Candidate({0: 1.0, 1: 2.0, 2: 2.0}, 0), False, 3),  # fails on the third
        (drsa._Candidate({0: 3.0, 1: 1.0, 2: 1.0}, 0), False, 1),  # fails on the first
    ]
    for general, expected, steps in cases:
        before = budget.used
        assert drsa._generalizes(general, specific, table, True, budget) is expected
        assert budget.used - before == steps


def test_students_operation_count_is_deterministic_and_complete() -> None:
    dataset = Dataset.model_validate(students())
    _, metrics = drsa.compute(dataset, drsa.DrsaParameters())
    _, again = drsa.compute(dataset, drsa.DrsaParameters())
    assert metrics["operations"] == again["operations"] > 128


# --- wall-clock deadline (abort only) ------------------------------------------------------------


def test_deadline_bounds_and_default_are_documented() -> None:
    assert meter.DEFAULT_DEADLINE_SECONDS == 300.0 and meter.MAX_DEADLINE_SECONDS == 3600.0
    assert AlgorithmAdapter(DRSA).deadline_seconds == 300.0
    assert AlgorithmAdapter(DRSA, deadline_seconds=None).deadline_seconds is None
    for bad in (0, -1.0, 3600.5, math.nan, math.inf, True):
        with pytest.raises(ValueError, match="ALGORITHM_DEADLINE_INVALID"):
            AlgorithmAdapter(DRSA, deadline_seconds=bad)
        with pytest.raises(AlgorithmError, match="ALGORITHM_DEADLINE_INVALID"):
            meter.Deadline(bad)


def test_deadline_aborts_drsa_inside_the_meter(clock: _Clock) -> None:
    clock.tick = 1.0
    deadline = meter.Deadline(5.0)  # one reading to start, then one per spend: the sixth spend is late
    budget = drsa.OperationBudget(drsa.DEFAULT_OPERATION_BUDGET, deadline=deadline)
    params = drsa.DrsaParameters()
    with pytest.raises(AlgorithmError) as caught:
        drsa.compute(Dataset.model_validate(students()), params, budget=budget, deadline=deadline)
    assert caught.value.code == "ALGORITHM_DEADLINE_EXCEEDED"
    # Readings: 1 at the start (expires at +5), then one per spend; the 6th spend reads +6 and aborts.
    # The first five spends are the table work (32 + 3 x 24 + 24 = 128), so the run stops before any cone.
    assert clock.readings == 1 + 6 and budget.used == 128
    # A meter attached to another deadline is refused (one clock per run).
    with pytest.raises(AlgorithmError, match="DRSA_OPERATION_BUDGET_INVALID"):
        drsa.compute(Dataset.model_validate(students()), params,
                     budget=drsa.OperationBudget(params.operation_budget), deadline=deadline)


def test_deadline_aborts_fcm_and_dematel(clock: _Clock) -> None:
    clock.tick = 1.0
    fcm_data = Dataset.model_validate(matrix(("A", "B"), [[0, 0.5], [-0.5, 0]]))
    with pytest.raises(AlgorithmError, match="ALGORITHM_DEADLINE_EXCEEDED"):
        fcm.compute(fcm_data, fcm.FcmParameters(initial_state={"A": 1, "B": 1}, epsilon=1e-12),
                    deadline=meter.Deadline(3.0))
    dematel_data = Dataset.model_validate(matrix(("A", "B", "C"), [[0, 3, 2], [1, 0, 1], [2, 4, 0]]))
    with pytest.raises(AlgorithmError, match="ALGORITHM_DEADLINE_EXCEEDED"):
        mcdm.compute_dematel(dematel_data, mcdm.DematelParameters(), deadline=meter.Deadline(10.0))


def test_adapter_deadline_aborts_with_a_closed_code_and_writes_nothing(tmp_path: Path, clock: _Clock) -> None:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    source = put_json(store, students())
    before = stored_objects(tmp_path / "objects")
    clock.tick = 1.0
    with pytest.raises(CapabilityAdapterError) as caught:
        run_stage(store, DRSA.capability, AlgorithmAdapter(DRSA, deadline_seconds=20.0), [source], {})
    assert caught.value.code == "ALGORITHM_DEADLINE_EXCEEDED"
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert stored_objects(tmp_path / "objects") == before  # fail closed: no partial output


@pytest.mark.parametrize("spec,document,parameters", [
    (DRSA, students(), {}),
    (FCM, matrix(("A", "B"), [[0, 0.5], [0, 0]]), {"initial_state": {"A": 1, "B": 1}}),
    (DEMATEL, matrix(("A", "B"), [[0, 3], [1, 0]]), {}),
], ids=["drsa", "fcm", "dematel"])
def test_a_deadline_never_changes_a_completed_result(tmp_path: Path, clock: _Clock, spec: Any, document: Any,
                                                     parameters: dict[str, Any]) -> None:
    """Frozen clock: the deadline never fires. The result bytes are identical with and without it; only
    the record's telemetry says that completion was subject to a wall-clock deadline."""
    clock.tick = 0.0
    outcomes = []
    for name, seconds in (("guarded", 60.0), ("unguarded", None)):
        store = ObjectStoreArtifacts(tmp_path / name)
        source = put_json(store, document)
        binding, output = run_bound(store, spec.capability, AlgorithmAdapter(spec, deadline_seconds=seconds),
                                    [source], parameters)
        result_ref, record_ref = output.outputs
        record = json.loads(store.get(record_ref))
        verdict = verify_replay(ExpectedStage.from_binding(binding, output), store.get(record_ref),
                                store.get(result_ref), store.get(source), parameters, deadline_seconds=seconds)
        assert verdict.code == "REPLAY_MATCH"
        outcomes.append((store.get(result_ref), record))
    (guarded_result, guarded_record), (plain_result, plain_record) = outcomes
    assert guarded_result == plain_result
    assert (guarded_record["deadline_seconds"], guarded_record["completion"]) == (60.0, "NONDETERMINISTIC_DEADLINE")
    assert (plain_record["deadline_seconds"], plain_record["completion"]) == (None, "DETERMINISTIC")
    assert guarded_record["result_hash"] == plain_record["result_hash"]
    assert guarded_record["metrics"] == plain_record["metrics"]


def test_replay_deadline_is_a_closed_verdict(tmp_path: Path, clock: _Clock) -> None:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    source = put_json(store, students())
    binding, output = run_bound(store, DRSA.capability, AlgorithmAdapter(DRSA, deadline_seconds=None), [source], {})
    result_ref, record_ref = output.outputs
    expected = ExpectedStage.from_binding(binding, output)
    args = (store.get(record_ref), store.get(result_ref), store.get(source), {})
    clock.tick = 1.0
    assert verify_replay(expected, *args, deadline_seconds=5.0).code == "REPLAY_ALGORITHM_DEADLINE_EXCEEDED"
    assert verify_replay(expected, *args, deadline_seconds=0.0).code == "REPLAY_ALGORITHM_DEADLINE_INVALID"
    assert verify_replay(expected, *args).code == "REPLAY_MATCH"  # no deadline: the clock is never read


def test_record_completion_must_match_its_deadline() -> None:
    base = {"algorithm_id": "drsa.domlem", "algorithm_version": "3", "capability_id": "ROUGH_SET_RULE_LEARNING",
            "adapter_id": "algorithms.drsa.domlem", "parameters_hash": "a" * 64, "request_parameters_hash": "a" * 64,
            "dataset_hash": "a" * 64, "dataset_artifact_sha256": "a" * 64, "result_hash": "a" * 64,
            "metrics": {}, "compute_target": "local-cpu"}
    assert ReproducibilityRecord.model_validate(base).completion is Completion.DETERMINISTIC
    good = {**base, "deadline_seconds": 5.0, "completion": "NONDETERMINISTIC_DEADLINE"}
    assert ReproducibilityRecord.model_validate(good).deadline_seconds == 5.0
    for bad in ({**base, "deadline_seconds": 5.0}, {**base, "completion": "NONDETERMINISTIC_DEADLINE"},
                {**base, "deadline_seconds": 3601.0, "completion": "NONDETERMINISTIC_DEADLINE"}):
        with pytest.raises(ValueError):
            ReproducibilityRecord.model_validate(bad)
