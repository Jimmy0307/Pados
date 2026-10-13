"""Herdr telemetry store: bounded R1, minimized, atomic 0600, stale never live, no terminal output."""
from __future__ import annotations

import itertools
import json
import os
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pados.herdr.adapter import CollectResult
from pados.herdr.contract import HerdrAgentState, HerdrHealth, HerdrTelemetryRecord
from pados.herdr.diagnostics import HerdrCollectionOutcome
from pados.herdr.store import (
    MAX_RECORDS_LIMIT,
    TERMINAL_HISTORY_DEFAULT,
    HerdrStoreError,
    HerdrTelemetryStore,
    observation_age_seconds,
)
from pados.lifecycle.minimization import DataMinimizationViolation
from pados.lifecycle.retention import DataKind, PayloadState, RetentionPolicy, policy_for
from tests.herdr.herdr_support import MACHINE, NOW, TERMINAL_MARKER, Clock, adapter_for, pane_ref, session_ref

_DIGESTS = itertools.count()


def _record(pane: str = "p-1", state: HerdrAgentState = HerdrAgentState.WORKING, *, at: datetime = NOW,
            session: str = "s-alpha", source_age: timedelta | None = timedelta(seconds=5),
            health: HerdrHealth = HerdrHealth.HEALTHY) -> HerdrTelemetryRecord:
    return HerdrTelemetryRecord(
        machine_ref=MACHINE, session_ref=session_ref(session), pane_ref=pane_ref(session, pane), state=state,
        collected_at=at, source_observed_at=None if source_age is None else at - source_age, health=health)


def _result(*records: HerdrTelemetryRecord, at: datetime | None = None,
            outcome: HerdrCollectionOutcome = HerdrCollectionOutcome.COLLECTED) -> CollectResult:
    return CollectResult(collected_at=at or (records[0].collected_at if records else NOW), outcome=outcome,
                         source_health=HerdrHealth.HEALTHY, records=records,
                         document_digest=f"{next(_DIGESTS):032x}")


def _store(tmp_path: Path, clock: Clock, **kwargs: object) -> HerdrTelemetryStore:
    return HerdrTelemetryStore(tmp_path / "herdr" / "telemetry.json", clock=clock, **kwargs)  # type: ignore[arg-type]


def test_store_is_r1_herdr_telemetry_and_terminal_history_is_not_persisted(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    assert store.record_collection(adapter_for("working.json", clock).collect()) is HerdrCollectionOutcome.COLLECTED
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert document["kind"] == DataKind.HERDR_TELEMETRY.value == policy_for(DataKind.HERDR_TELEMETRY).kind.value
    assert document["retention_class"] == "R1_OPERATIONAL" and document["semantic_authority"] == "NONE"
    assert set(document) == {"schema_id", "kind", "retention_class", "semantic_authority", "records",
                             "collections", "last_document_digest"}
    assert TERMINAL_HISTORY_DEFAULT is PayloadState.NOT_PERSISTED
    assert store.retention == RetentionPolicy().r1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_store_file_is_owner_only_and_written_atomically(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.record_collection(_result(_record()))
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    leftovers = [p.name for p in store.path.parent.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_symlinked_store_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "herdr" / "telemetry.json"
    link.parent.mkdir()
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    store = HerdrTelemetryStore(link, clock=Clock())
    with pytest.raises(OSError):
        store.record_collection(_result(_record()))
    assert store.records() == [] and target.read_text(encoding="utf-8") == "{}"


def test_terminal_output_is_never_stored(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.record_collection(adapter_for("terminal_content.json", clock).collect())
    text = store.path.read_text(encoding="utf-8")
    assert TERMINAL_MARKER not in text
    for word in ("scrollback", "buffer", "output", "history", "screen", "content"):
        assert word not in text


def test_growth_is_bounded_by_record_count(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock, max_records=50)
    for index in range(30):
        clock.advance(seconds=1)
        store.record_collection(_result(*(_record(f"p-{index}-{n}", at=clock.now) for n in range(5))))
    rows = store.records()
    assert len(rows) == 50 and rows[-1]["pane_ref"] == pane_ref("s-alpha", "p-29-4")


def test_growth_is_bounded_by_bytes(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock, max_records=MAX_RECORDS_LIMIT, max_bytes=16 * 1024)
    for index in range(200):
        store.record_collection(_result(_record(f"p-{index}")))
    assert store.path.stat().st_size <= 16 * 1024 and 0 < len(store.records()) < 200
    assert store.records()[-1]["pane_ref"] == pane_ref("s-alpha", "p-199")


def test_r1_age_pruning_happens_on_write(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.record_collection(_result(_record("p-old", at=clock.now)))
    clock.advance(days=RetentionPolicy().r1.days + 1)
    store.record_collection(_result(_record("p-new", at=clock.now)))
    document = json.loads(store.path.read_text(encoding="utf-8"))
    assert [row["pane_ref"] for row in document["records"]] == [pane_ref("s-alpha", "p-new")]
    assert len(document["collections"]) == 1


def test_retention_can_be_shortened_but_never_exceed_r1(tmp_path: Path) -> None:
    clock = Clock()
    assert _store(tmp_path, clock, retention=timedelta(days=90)).retention == RetentionPolicy().r1
    short = _store(tmp_path, clock, retention=timedelta(hours=1))
    short.record_collection(_result(_record("p-1", at=clock.now)))
    clock.advance(hours=2)
    assert short.records() == [] and short.prune() == 1
    with pytest.raises(HerdrStoreError):
        _store(tmp_path, clock, retention=timedelta(0))


@pytest.mark.parametrize(("kwargs", "code"), [
    ({"max_records": 0}, "HERDR_STORE_MAX_RECORDS_OUT_OF_RANGE"),
    ({"max_records": MAX_RECORDS_LIMIT + 1}, "HERDR_STORE_MAX_RECORDS_OUT_OF_RANGE"),
    ({"max_records": True}, "HERDR_STORE_MAX_RECORDS_OUT_OF_RANGE"),
    ({"max_bytes": 100}, "HERDR_STORE_MAX_BYTES_OUT_OF_RANGE"),
])
def test_store_bounds_are_validated(tmp_path: Path, kwargs: dict[str, object], code: str) -> None:
    with pytest.raises(HerdrStoreError, match=code):
        _store(tmp_path, Clock(), **kwargs)


def test_latest_projection_is_per_machine_session_pane(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.record_collection(_result(_record("p-1", HerdrAgentState.WORKING, at=clock.now - timedelta(seconds=30))))
    store.record_collection(_result(
        _record("p-1", HerdrAgentState.IDLE, at=clock.now - timedelta(seconds=10)),
        _record("p-1", HerdrAgentState.BLOCKED, at=clock.now - timedelta(seconds=10), session="s-beta"),
        _record("p-2", HerdrAgentState.EXITED, at=clock.now - timedelta(seconds=10))))
    latest = {row["pane_ref"]: row for row in store.latest(stale_after_seconds=60)}
    assert {key: row["state"] for key, row in latest.items()} == {
        pane_ref("s-alpha", "p-1"): "IDLE", pane_ref("s-beta", "p-1"): "BLOCKED", pane_ref("s-alpha", "p-2"): "EXITED"}
    row = latest[pane_ref("s-alpha", "p-1")]
    assert row["stale"] is False and row["stale_reason"] is None
    assert row["telemetry_meaning"] == "AGENT_IDLE_OBSERVED"
    assert row["meaning_scope"] == "TELEMETRY_ONLY" and row["semantic_authority"] == "NONE"


def test_stale_rows_are_never_shown_as_live(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.record_collection(_result(_record("p-1", HerdrAgentState.WORKING, at=clock.now)))
    clock.advance(seconds=121)
    (row,) = store.latest(stale_after_seconds=120)
    assert row["stale"] is True and row["state"] == "UNKNOWN" and row["health"] is None
    assert row["last_observed_state"] == "WORKING" and row["stale_reason"] == "SOURCE_TIME_EXPIRED"
    assert row["telemetry_meaning"] == "AGENT_STATE_NOT_OBSERVED"
    (forced,) = store.latest(stale_after_seconds=86_400, not_installed=True)
    assert forced["stale"] is True and forced["state"] == "UNKNOWN"
    assert forced["stale_reason"] == "SOURCE_NOT_INSTALLED"


def test_unverifiable_times_are_stale_never_fresh(tmp_path: Path) -> None:
    now = NOW
    for value in (None, "", "garbage", "2026-10-02T10:00:00", (now + timedelta(minutes=5)).isoformat(), 7):
        assert observation_age_seconds(value, now) is None
    assert observation_age_seconds((now + timedelta(seconds=30)).isoformat(), now) == 0.0
    clock = Clock()
    store = _store(tmp_path, clock)
    store.record_collection(_result(_record("p-1", at=clock.now, source_age=timedelta(0))))
    clock.advance(minutes=-10)  # the reader's clock is behind: the source time looks future-dated
    (row,) = store.latest(stale_after_seconds=86_400)
    assert row["stale"] is True and row["source_age_seconds"] is None and row["state"] == "UNKNOWN"
    with pytest.raises(HerdrStoreError):
        store.latest(stale_after_seconds=0)


def test_future_dated_rows_are_pruned_on_write(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.record_collection(_result(_record("p-future", at=clock.now + timedelta(hours=1)),
                                    _record("p-now", at=clock.now), at=clock.now))
    assert [row["pane_ref"] for row in store.records()] == [pane_ref("s-alpha", "p-now")]


def test_tampered_or_malformed_rows_on_disk_are_ignored_and_pruned(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.record_collection(_result(_record("p-1", at=clock.now)))
    document = json.loads(store.path.read_text(encoding="utf-8"))
    good = document["records"][0]
    document["records"] += [
        {**good, "pane_ref": pane_ref("s", "2"), "pados_status": "ACCEPTED"},
        {**good, "pane_ref": pane_ref("s", "3"), "state": "ACCEPTED"},
        {**good, "pane_ref": pane_ref("s", "4"), "semantic_authority": "FULL"},
        {**good, "pane_ref": pane_ref("s", "5"), "scrollback": [TERMINAL_MARKER]},
        {**good, "pane_ref": pane_ref("s", "6"), "machine_ref": "workstation-01"},
        {**good, "pane_ref": "p-raw-id"},
        "not-a-row", 42, None,
    ]
    document["collections"] += [{"at": NOW.isoformat(), "outcome": "ACCEPTED", "code": None},
                                {"at": NOW.isoformat(), "outcome": "COLLECTED", "code": TERMINAL_MARKER}]
    store.path.write_text(json.dumps(document), encoding="utf-8")
    assert [row["pane_ref"] for row in store.records()] == [pane_ref("s-alpha", "p-1")]
    assert len(store.collections()) == 1
    assert store.prune() == 9
    assert TERMINAL_MARKER not in store.path.read_text(encoding="utf-8")


def test_corrupt_store_reads_as_empty_and_recovers_on_write(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{corrupt", encoding="utf-8")
    assert store.records() == [] and store.latest() == [] and store.latest_collection() is None
    store.record_collection(_result(_record()))
    assert len(store.records()) == 1


def test_records_that_fail_minimization_are_never_written(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    credential_like = "ghp_" + "Z9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J3i2"
    bad = _record().model_copy(update={"project_ref": credential_like})
    with pytest.raises(DataMinimizationViolation):
        store.record_collection(_result(_record("p-ok"), bad))
    assert not store.path.exists()  # all-or-nothing: the valid record was not written either


def test_reads_never_write(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    assert store.records() == [] and store.latest() == [] and store.prune() == 0
    assert store.collections() == [] and store.latest_collection() is None
    assert not store.path.parent.exists()
