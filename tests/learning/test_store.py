"""Outcome/evaluation stores: append-only JSONL, owner-only, R1 pruning, bounded, fail closed."""
from __future__ import annotations

import json
import multiprocessing
import os
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pados.learning.records import OutcomeRecord
from pados.learning.store import EvaluationStore, LearningStoreError, OutcomeStore
from pados.lifecycle.retention import RetentionPolicy
from tests.learning.learning_support import AUTH, NOW, outcome
from tests.learning.test_records import evaluation


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def store(tmp_path: Path, clock: Clock | None = None, **kwargs: object) -> OutcomeStore:
    return OutcomeStore(tmp_path / "learning" / "outcomes.jsonl", authenticator=AUTH,  # type: ignore[arg-type]
                        clock=clock or Clock(NOW), **kwargs)


def lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_append_read_and_idempotency(tmp_path: Path) -> None:
    outcomes = store(tmp_path)
    first = outcomes.append(outcome(1))
    assert first.appended and first.pruned == 0
    assert outcomes.append(outcome(1)).appended is False  # identical record: no-op
    outcomes.append(outcome(2))
    assert [r.outcome_id for r in outcomes.read()] == [outcome(2).outcome_id, outcome(1).outcome_id]
    assert len(lines(outcomes.path)) == 2
    changed = OutcomeRecord.model_validate({**outcome(1).model_dump(), "cost_units": 5.0})
    with pytest.raises(LearningStoreError, match="LEARNING_RECORD_ID_CONFLICT"):
        outcomes.append(changed)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_files_are_owner_only(tmp_path: Path) -> None:
    outcomes = store(tmp_path)
    outcomes.append(outcome(1))
    assert stat.S_IMODE(os.stat(outcomes.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(outcomes.path.parent).st_mode) == 0o700
    os.chmod(outcomes.path, 0o644)  # an insecure file is refused, never silently repaired
    with pytest.raises(LearningStoreError, match="LEARNING_STORE_INSECURE"):
        outcomes.append(outcome(2))


def test_r1_retention_hides_on_read_and_deletes_only_on_operator_prune(tmp_path: Path) -> None:
    """CR-11d OL-14: reads and appends never delete (no autonomous deletion); ``prune()`` is the only path."""
    clock = Clock(NOW)
    outcomes = store(tmp_path, clock)
    outcomes.append(outcome(1))
    assert outcomes.window == timedelta(days=14)
    clock.now = NOW + timedelta(days=15)
    assert outcomes.read() == ()
    assert len(lines(outcomes.path)) == 1  # hidden, not deleted
    outcomes.append(outcome(1, at=clock.now - timedelta(days=13)))
    clock.now += timedelta(days=2)
    result = outcomes.append(outcome(2, at=clock.now))
    assert result.appended and result.pruned == 0 and len(lines(outcomes.path)) == 3
    assert [r.outcome_id for r in outcomes.read()] == [outcome(2, at=clock.now).outcome_id]
    assert outcomes.prune() == 2  # the operator's explicit purge
    assert [line["outcome_id"] for line in lines(outcomes.path)] == [outcome(2, at=clock.now).outcome_id]
    with pytest.raises(LearningStoreError, match="OUTCOME_RECORD_ALREADY_EXPIRED"):
        outcomes.append(outcome(3))
    with pytest.raises(LearningStoreError, match="OUTCOME_RECORDED_IN_FUTURE"):
        outcomes.append(outcome(4, at=clock.now + timedelta(hours=1)))


def test_retention_policy_controls_the_window(tmp_path: Path) -> None:
    clock = Clock(NOW)
    outcomes = store(tmp_path, clock, retention=RetentionPolicy.configured(r1_days=7))
    outcomes.append(outcome(1))
    clock.now = NOW + timedelta(days=8)
    assert outcomes.prune() == 1 and outcomes.read() == ()
    with pytest.raises(ValueError, match="R1_WINDOW_REQUIRES_EXPLICIT_OVERRIDE"):
        RetentionPolicy.configured(r1_days=90)


def test_store_is_bounded_and_a_full_store_refuses_instead_of_deleting(tmp_path: Path) -> None:
    outcomes = store(tmp_path, max_records=3)
    for index in range(3):
        outcomes.append(outcome(index))
    for index in (3, 9):
        with pytest.raises(LearningStoreError, match="^OUTCOME_STORE_FULL_PRUNE_REQUIRED$"):
            outcomes.append(outcome(index))  # never displaces a stored record
    assert {r.outcome_id for r in outcomes.read()} == {outcome(i).outcome_id for i in range(3)}


def test_out_of_order_append_keeps_chronological_file(tmp_path: Path) -> None:
    outcomes = store(tmp_path)
    outcomes.append(outcome(1))
    outcomes.append(outcome(5))
    assert [r.outcome_id for r in outcomes.read()] == [outcome(5).outcome_id, outcome(1).outcome_id]


def test_corrupt_or_foreign_lines_fail_closed(tmp_path: Path) -> None:
    outcomes = store(tmp_path)
    outcomes.append(outcome(1))
    with outcomes.path.open("a") as handle:
        handle.write(json.dumps({**outcome(2).to_record(), "prompt": "summarize"}) + "\n")
    with pytest.raises(LearningStoreError, match="LEARNING_STORE_CORRUPT"):
        outcomes.read()
    with pytest.raises(LearningStoreError, match="LEARNING_STORE_CORRUPT"):
        outcomes.append(outcome(3))
    outcomes.path.write_text("not json\n")
    with pytest.raises(LearningStoreError, match="LEARNING_STORE_CORRUPT"):
        outcomes.read()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_links_are_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("")
    path = tmp_path / "learning" / "outcomes.jsonl"
    path.parent.mkdir(mode=0o700)
    path.symlink_to(target)
    with pytest.raises(LearningStoreError, match="LEARNING_STORE_NOT_REGULAR_FILE"):
        OutcomeStore(path, authenticator=AUTH, clock=Clock(NOW)).append(outcome(1))
    assert target.read_text() == ""


def test_persisted_lines_hold_metadata_only(tmp_path: Path) -> None:
    outcomes = store(tmp_path)
    outcomes.append(outcome(1, quality=0.8))
    raw = outcomes.path.read_text()
    for forbidden in ("prompt", "document_text", "messages", "page_text"):
        assert forbidden not in raw
    assert set(lines(outcomes.path)[0]) == set(outcome(1).to_record())


def _append_many(path: str, start: int) -> None:
    target = OutcomeStore(Path(path), authenticator=AUTH, clock=lambda: NOW)
    for index in range(start, start + 15):
        target.append(outcome(index))


def test_cross_process_appends_do_not_lose_or_corrupt_lines(tmp_path: Path) -> None:
    path = tmp_path / "learning" / "outcomes.jsonl"
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_append_many, args=(str(path), start)) for start in (0, 100)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
        assert worker.exitcode == 0
    for index in (*range(15), *range(100, 115)):
        outcome(index)  # the parent's receipt store knows the same trusted receipts
    records = OutcomeStore(path, authenticator=AUTH, clock=lambda: NOW).read()
    assert len(records) == 30 and len({r.outcome_id for r in records}) == 30


def test_evaluation_store_is_r2_never_pruned_and_refuses_when_full(tmp_path: Path) -> None:
    evaluations = EvaluationStore(tmp_path / "learning" / "evaluations.jsonl", max_records=2, clock=lambda: NOW)
    first = evaluation()
    assert evaluations.append(first).appended
    assert not evaluations.append(first).appended
    evaluations.append(evaluation(metric_name="word_accuracy"))
    with pytest.raises(LearningStoreError, match="EVALUATION_STORE_FULL"):
        evaluations.append(evaluation(metric_name="layout_f1"))
    assert [r.metric_name for r in evaluations.read()] == ["char_accuracy", "word_accuracy"]
    old = evaluation(evaluated_at=NOW - timedelta(days=400), metric_name="cer")
    assert EvaluationStore(tmp_path / "e2.jsonl", clock=lambda: NOW).append(old).appended  # no R2 age window


def test_byte_bound_refuses_and_parse_cache_sees_other_writers(tmp_path: Path) -> None:
    path = tmp_path / "learning" / "outcomes.jsonl"
    small = OutcomeStore(path, authenticator=AUTH, clock=Clock(NOW), max_bytes=16 * 1024)  # ~20 lines
    reader = OutcomeStore(path, authenticator=AUTH, clock=Clock(NOW))
    refused = 0
    for index in range(40):
        try:
            small.append(outcome(index))
        except LearningStoreError as exc:
            assert exc.code == "OUTCOME_STORE_FULL_PRUNE_REQUIRED"
            refused += 1
    assert path.stat().st_size <= small.max_bytes and refused > 0
    before = reader.read()
    assert 0 < len(before) < 40 and before[-1].outcome_id == outcome(0).outcome_id
    other = OutcomeStore(path, authenticator=AUTH, clock=Clock(NOW + timedelta(minutes=1)))
    other.append(outcome(0, at=NOW + timedelta(minutes=1), adapter_id="ocr.engine-b"))
    assert reader.read()[-1].adapter_id == "ocr.engine-b"  # cache invalidated by the other writer
