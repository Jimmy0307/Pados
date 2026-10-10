"""CR-06 fixer round: one or more deterministic tests per accepted finding (H01-H03, M01-M05)."""
from __future__ import annotations

import base64
import itertools
import json
import os
import re
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pados.herdr import adapter as adapter_module
from pados.herdr import store as store_module
from pados.herdr.adapter import (
    MAX_ENTRIES,
    CollectResult,
    CommandHerdrSource,
    FixtureHerdrSource,
    HerdrAdapter,
    HerdrBinding,
    HerdrSourceError,
    bounded_run,
)
from pados.herdr.config import HerdrConfig, load_config
from pados.herdr.contract import (
    PANE_REF_PATTERN,
    SESSION_REF_PATTERN,
    HerdrAgentKind,
    HerdrAgentState,
    HerdrHealth,
    HerdrTelemetryRecord,
)
from pados.herdr.diagnostics import HerdrCollectionOutcome, HerdrDiagnostic, diagnostic
from pados.herdr.store import MAX_COLLECTIONS, REF_KEY_SUFFIX, HerdrStoreError, HerdrTelemetryStore
from pados.herdr.visibility import herdr_visibility
from pados.lifecycle.inventory import INVENTORY
from tests.herdr.herdr_support import (
    LABEL_MARKER,
    MACHINE,
    NOW,
    TEST_KEY,
    Clock,
    adapter_for,
    fixture,
    fresh_fixture,
    pane_doc,
    pane_ref,
    raw,
    session_ref,
    write_config,
)

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups / permission bits")
WAITID_ONLY = pytest.mark.skipif(adapter_module._waitid_primitives() is None,
                                reason="non-reaping waitid is unavailable")
_DIGESTS = itertools.count(1000)


def _store(tmp_path: Path, clock: Clock, **kwargs: Any) -> HerdrTelemetryStore:
    return HerdrTelemetryStore(tmp_path / "herdr" / "telemetry.json", clock=clock, **kwargs)


def _everything(store: HerdrTelemetryStore, result: CollectResult) -> str:
    return (store.path.read_text(encoding="utf-8") + json.dumps(store.latest()) + json.dumps(store.collections())
            + json.dumps(result.summary()))


# --- H01: no source-controlled free text in the store or projections ------------------------------

def test_cr06_h01_source_free_text_never_reaches_store_or_projection(tmp_path: Path) -> None:
    clock = Clock()
    secret_label = "customer " + "pass" + "word hunter2 " + LABEL_MARKER
    encoded = base64.b32encode(b"terminal secret text").decode().rstrip("=")
    document = pane_doc(
        {"pane_id": encoded, "state": "working", "agent": {"kind": "claude", "label": secret_label},
         "project_ref": "proj-" + encoded[:20], "work_package_ref": "wp-" + encoded[:10], "tag": encoded},
        session_id="sess-" + encoded, snapshot_at=NOW - timedelta(seconds=5))
    adapter = adapter_for("idle.json", clock)
    result = adapter.translate(raw(document))
    (record,) = result.records
    assert record.session_ref == session_ref("sess-" + encoded) and record.pane_ref.startswith("hp-")
    assert record.project_ref is None and record.work_package_ref is None
    store = _store(tmp_path, clock)
    store.record_collection(result)
    text = _everything(store, result)
    for planted in (LABEL_MARKER, "hunter2", "customer", encoded[:12], "sess-", "proj-"):
        assert planted not in text, planted


def test_cr06_h01_fixture_labels_and_raw_refs_are_not_persisted(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    result = adapter_for("working.json", clock).collect()
    store.record_collection(result)
    text = _everything(store, result)
    for planted in (LABEL_MARKER, "Claude Code", "s-alpha", "billing-service", "proj-demo", "wp-0001"):
        assert planted not in text, planted


def test_cr06_h01_refs_are_keyed_digests_stable_per_key(tmp_path: Path) -> None:
    other_key = bytes(reversed(range(32)))
    first = adapter_for("multi_session.json").collect().records
    again = adapter_for("multi_session.json").collect().records
    other = adapter_for("multi_session.json", key=other_key).collect().records
    assert [r.pane_ref for r in first] == [r.pane_ref for r in again]
    assert not {r.pane_ref for r in first} & {r.pane_ref for r in other}
    for record in first:
        assert re.fullmatch(SESSION_REF_PATTERN, record.session_ref)
        assert re.fullmatch(PANE_REF_PATTERN, record.pane_ref)
    # the same pane id in two sessions is two different panes
    document = {"snapshot_at": NOW.isoformat(), "sessions": [
        {"session_id": "a", "panes": [{"pane_id": "1", "state": "idle"}]},
        {"session_id": "b", "panes": [{"pane_id": "1", "state": "idle"}]}]}
    refs = {r.pane_ref for r in adapter_for("idle.json").translate(raw(document)).records}
    assert len(refs) == 2


@POSIX_ONLY
def test_cr06_h01_ref_key_is_a_private_random_file_created_on_first_use(tmp_path: Path) -> None:
    store = _store(tmp_path, Clock())
    assert not store.key_path.exists()
    key = store.ref_key()
    assert store.key_path.name == store.path.name + REF_KEY_SUFFIX
    assert len(key) == 32 and store.ref_key() == key
    assert stat.S_IMODE(os.stat(store.key_path).st_mode) == 0o600
    other = HerdrTelemetryStore(tmp_path / "other" / "telemetry.json").ref_key()
    assert other != key
    adapter = HerdrAdapter(FixtureHerdrSource(fixture("working.json")), machine_ref=MACHINE, ref_key=key,
                           clock=Clock())
    result = adapter.collect()
    store.record_collection(result)
    text = _everything(store, result)
    assert key.hex() not in text and base64.b64encode(key).decode() not in text


@POSIX_ONLY
def test_cr06_h01_unsafe_ref_key_files_are_refused(tmp_path: Path) -> None:
    store = _store(tmp_path, Clock())
    store.ref_key()
    os.chmod(store.key_path, 0o644)
    with pytest.raises(HerdrStoreError, match="HERDR_REF_KEY_PERMISSIONS"):
        store.ref_key()
    os.chmod(store.key_path, 0o600)
    store.key_path.write_bytes(b"short")
    with pytest.raises(HerdrStoreError, match="HERDR_REF_KEY_INVALID"):
        store.ref_key()
    store.key_path.unlink()
    target = tmp_path / "planted.key"
    target.write_bytes(bytes(32))
    store.key_path.symlink_to(target)
    with pytest.raises(HerdrStoreError, match="HERDR_REF_KEY_INVALID"):
        store.ref_key()


def test_cr06_h01_project_refs_come_only_from_operator_bindings() -> None:
    bindings = {"billing-service": HerdrBinding("proj-billing", "wp-0042")}
    (bound,) = adapter_for("working.json", bindings=bindings).collect().records
    assert (bound.project_ref, bound.work_package_ref) == ("proj-billing", "wp-0042")
    result = adapter_for("working.json").collect()  # no bindings: the raw project_ref claim is ignored
    (unbound,) = result.records
    assert unbound.project_ref is None and unbound.work_package_ref is None
    assert result.authority_claims_ignored >= 2  # project_ref, work_package_ref


def test_cr06_h01_config_bindings_reach_the_adapter(tmp_path: Path) -> None:
    path = write_config(tmp_path, fixture_path=str(fresh_fixture(tmp_path, "working.json")),
                        bindings={"billing-service": {"project_ref": "proj-billing"}})
    config = load_config(str(path))
    assert isinstance(config, HerdrConfig)
    config.collect()
    (row,) = config.store().records()
    assert row["project_ref"] == "proj-billing" and row["work_package_ref"] is None


def test_cr06_h01_inventory_entry_names_the_real_writer_and_holds_no_prohibited_data() -> None:
    (point,) = [p for p in INVENTORY if p.point_id == "herdr_telemetry"]
    assert point.may_hold_prohibited is False and point.risks == ()
    module, cls, method = point.writer.rsplit(".", 2)
    assert module == "pados.herdr.store" and getattr(getattr(store_module, cls), method)


# --- M05: no source-controlled descriptive labels in authority-facing projections -------------------

@pytest.mark.parametrize("label", ["PADOS ACCEPTED", "audit verified", "Approved by reviewer", "ACCEPTED"])
def test_cr06_m05_authority_sounding_labels_never_appear(tmp_path: Path, label: str) -> None:
    clock = Clock()
    document = pane_doc({"pane_id": "p-1", "state": "idle", "agent": {"kind": "codex", "label": label}},
                        snapshot_at=NOW - timedelta(seconds=5))
    result = adapter_for("idle.json", clock).translate(raw(document))
    (record,) = result.records
    assert record.agent_kind is HerdrAgentKind.CODEX
    store = _store(tmp_path, clock)
    store.record_collection(result)
    assert label not in _everything(store, result)
    (row,) = store.latest()
    assert set(row) >= {"agent_kind"} and "label" not in json.dumps(row)


@pytest.mark.parametrize(("kind", "expected"), [
    ("claude", HerdrAgentKind.CLAUDE_CODE), ("Claude-Code", HerdrAgentKind.CLAUDE_CODE),
    ("codex", HerdrAgentKind.CODEX), ("zsh", HerdrAgentKind.SHELL), ("PADOS ACCEPTED", HerdrAgentKind.OTHER),
    ("x" * 200, HerdrAgentKind.OTHER), (None, HerdrAgentKind.UNKNOWN), (42, HerdrAgentKind.OTHER),
])
def test_cr06_m05_agent_kind_is_mapped_from_known_values_only(kind: object, expected: HerdrAgentKind) -> None:
    document = pane_doc({"pane_id": "p-1", "state": "idle", "agent": kind})
    (record,) = adapter_for("idle.json").translate(raw(document)).records
    assert record.agent_kind is expected


# --- H02: collection time vs. proven source time ------------------------------------------------------

def test_cr06_h02_collection_time_is_separate_from_source_time() -> None:
    clock = Clock()
    (record,) = adapter_for("idle.json", clock).collect().records
    assert record.collected_at == NOW and record.source_observed_at == NOW - timedelta(seconds=30)


@pytest.mark.parametrize(("snapshot", "code"), [
    (None, HerdrDiagnostic.SOURCE_TIME_MISSING),
    ("yesterday", HerdrDiagnostic.SOURCE_TIME_INVALID),
    ("2026-10-02T09:59:59", HerdrDiagnostic.SOURCE_TIME_INVALID),  # naive
    (42, HerdrDiagnostic.SOURCE_TIME_INVALID),
    ((NOW + timedelta(minutes=5)).isoformat(), HerdrDiagnostic.SOURCE_TIME_IN_FUTURE),
    ((NOW - timedelta(minutes=10)).isoformat(), HerdrDiagnostic.SOURCE_TIME_TOO_OLD),
])
def test_cr06_h02_unproven_source_time_is_never_projected_live(tmp_path: Path, snapshot: object,
                                                               code: HerdrDiagnostic) -> None:
    clock = Clock()
    document = pane_doc({"pane_id": "p-1", "state": "working"}, snapshot_at=None)
    if snapshot is not None:
        document["snapshot_at"] = snapshot
    result = adapter_for("idle.json", clock).translate(raw(document))
    (record,) = result.records
    assert record.source_observed_at is None and result.source_time_code is code
    assert result.source_health is HerdrHealth.DEGRADED
    store = _store(tmp_path, clock)
    store.record_collection(result)
    (row,) = store.latest()
    assert row["stale"] is True and row["state"] == "UNKNOWN" and row["last_observed_state"] == "WORKING"
    assert row["stale_reason"] == "SOURCE_TIME_UNPROVEN"
    assert store.latest_collection() == {"at": NOW.isoformat(), "outcome": "COLLECTED", "code": code.value}


def test_cr06_h02_fixture_without_source_time_is_unproven(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.record_collection(adapter_for("no_source_time.json", clock).collect())
    (row,) = store.latest()
    assert row["stale"] is True and row["stale_reason"] == "SOURCE_TIME_UNPROVEN"


def test_cr06_h02_repeated_document_does_not_refresh_freshness(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    adapter = adapter_for("idle.json", clock, stale_after_seconds=120)
    assert store.record_collection(adapter.collect()) is HerdrCollectionOutcome.COLLECTED
    (row,) = store.latest(stale_after_seconds=120)
    assert row["stale"] is False
    for _ in range(5):
        clock.advance(seconds=60)
        assert store.record_collection(adapter.collect()) is HerdrCollectionOutcome.UNCHANGED
    assert len(store.records()) == 1  # nothing appended for a repeated document
    (row,) = store.latest(stale_after_seconds=120)
    assert row["stale"] is True and row["stale_reason"] == "SOURCE_TIME_EXPIRED"
    assert [c["outcome"] for c in store.collections()] == ["COLLECTED"] + ["UNCHANGED"] * 5


def test_cr06_h02_cached_old_document_refetched_later_is_never_live(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    clock.advance(minutes=30)  # the fixture's snapshot is now 30 minutes old
    store.record_collection(adapter_for("working.json", clock).collect())
    (row,) = store.latest()
    assert row["stale"] is True and row["state"] == "UNKNOWN"


def test_cr06_h02_unreachable_pane_health_is_never_live(tmp_path: Path) -> None:
    clock = Clock()
    document = pane_doc({"pane_id": "p-1", "state": "working", "health": "unreachable"},
                        snapshot_at=NOW - timedelta(seconds=5))
    store = _store(tmp_path, clock)
    store.record_collection(adapter_for("idle.json", clock).translate(raw(document)))
    (row,) = store.latest()
    assert row["stale"] is True and row["stale_reason"] == "SOURCE_HEALTH_UNREACHABLE"


# --- H03: command source disabled on Windows --------------------------------------------------------

def test_cr06_h03_command_source_fails_closed_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not execute")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(sys, "platform", "win32")
    source = CommandHerdrSource(which=lambda name: "C:\\herdr\\herdr.exe")
    assert source.installed() is False
    with pytest.raises(HerdrSourceError) as caught:
        source.fetch()
    assert caught.value.code is HerdrDiagnostic.COMMAND_SOURCE_UNSUPPORTED_ON_WINDOWS
    with pytest.raises(HerdrSourceError) as direct:
        bounded_run(("anything",), timeout_seconds=1, max_bytes=10)
    assert direct.value.code is HerdrDiagnostic.COMMAND_SOURCE_UNSUPPORTED_ON_WINDOWS
    result = HerdrAdapter(source, machine_ref=MACHINE, ref_key=TEST_KEY).collect()
    assert result.outcome is HerdrCollectionOutcome.SOURCE_UNREACHABLE
    assert result.error_code is HerdrDiagnostic.COMMAND_SOURCE_UNSUPPORTED_ON_WINDOWS


# --- M01: persisted collection outcome; failed latest collection forces stale ------------------------

class _FlakySource:
    def __init__(self, document: bytes) -> None:
        self.document = document
        self.failing = False

    def installed(self) -> bool:
        return True

    def fetch(self) -> bytes:
        if self.failing:
            raise HerdrSourceError(HerdrDiagnostic.SOURCE_TIMEOUT)
        return self.document


def test_cr06_m01_failed_latest_collection_forces_projection_stale(tmp_path: Path) -> None:
    clock = Clock()
    source = _FlakySource(raw(pane_doc({"pane_id": "p-1", "state": "working"},
                                       snapshot_at=NOW - timedelta(seconds=5))))
    adapter = HerdrAdapter(source, machine_ref=MACHINE, ref_key=TEST_KEY, clock=clock)
    store = _store(tmp_path, clock)
    store.record_collection(adapter.collect())
    assert store.latest()[0]["stale"] is False
    source.failing = True
    assert store.record_collection(adapter.collect()) is HerdrCollectionOutcome.SOURCE_UNREACHABLE
    (row,) = store.latest()
    assert row["stale"] is True and row["stale_reason"] == "LATEST_COLLECTION_UNHEALTHY"
    assert store.latest_collection() == {"at": NOW.isoformat(), "outcome": "SOURCE_UNREACHABLE",
                                         "code": "HERDR_SOURCE_TIMEOUT"}
    source.failing = False
    store.record_collection(adapter.collect())  # same document again: recovers the pipe, not freshness
    assert store.latest_collection()["outcome"] == "UNCHANGED"
    assert store.latest()[0]["stale"] is False  # source time is still within stale_after


def test_cr06_m01_collection_log_is_bounded_and_content_free(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    for index in range(MAX_COLLECTIONS * 3):
        clock.advance(seconds=1)
        store.record_collection(CollectResult(collected_at=clock.now, outcome=HerdrCollectionOutcome.DOCUMENT_REFUSED,
                                              source_health=HerdrHealth.DEGRADED,
                                              error_code=HerdrDiagnostic.RAW_DOC_INVALID_JSON))
    collections = store.collections()
    assert len(collections) == MAX_COLLECTIONS
    assert all(set(c) == {"at", "outcome", "code"} for c in collections)
    assert collections[-1]["at"] == clock.now.isoformat()


@POSIX_ONLY
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file modes")
def test_cr06_m01_installed_is_separate_from_runtime_availability(tmp_path: Path) -> None:
    source = fresh_fixture(tmp_path, "multi_session.json")
    path = write_config(tmp_path, fixture_path=str(source))
    config = load_config(str(path))
    assert isinstance(config, HerdrConfig)
    config.collect()
    assert herdr_visibility(str(path))["agents_live"] == 4
    os.chmod(source, 0)
    try:
        assert config.collect() is HerdrCollectionOutcome.SOURCE_UNREACHABLE
        view = herdr_visibility(str(path))
    finally:
        os.chmod(source, 0o600)
    assert view["status"] == "HERDR_UNAVAILABLE" and view["source_installed"] is True
    assert view["source_runtime"] == "UNREACHABLE" and view["agents_live"] == 0
    assert view["latest_collection"]["code"] == "HERDR_FIXTURE_UNREADABLE"
    source.write_text("{not json", encoding="utf-8")
    config.collect()
    degraded = herdr_visibility(str(path))
    assert degraded["status"] == "HERDR_DEGRADED" and degraded["source_runtime"] == "DOCUMENT_REFUSED"
    assert degraded["agents_live"] == 0


# --- M02: bounded store admission ------------------------------------------------------------------

def test_cr06_m02_oversized_store_file_is_never_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    store = _store(tmp_path, clock, max_bytes=8 * 1024)
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"records": [], "padding": "x" * 20_000}), encoding="utf-8")

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("an oversized store file must not be parsed")

    monkeypatch.setattr(store_module.json, "loads", refuse)
    assert store.records() == [] and store.latest() == [] and store.collections() == []
    monkeypatch.undo()
    record = HerdrTelemetryRecord(machine_ref=MACHINE, session_ref=session_ref("s"), pane_ref=pane_ref("s", "p"),
                                  collected_at=NOW)
    store.record_collection(CollectResult(collected_at=NOW, outcome=HerdrCollectionOutcome.COLLECTED,
                                          source_health=HerdrHealth.HEALTHY, records=(record,),
                                          document_digest=f"{next(_DIGESTS):032x}"))
    assert store.path.stat().st_size <= 8 * 1024 and len(store.records()) == 1


def test_cr06_m02_stored_arrays_over_the_record_cap_are_truncated_deterministically(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock, max_records=10)
    rows = [HerdrTelemetryRecord(machine_ref=MACHINE, session_ref=session_ref("s"), pane_ref=pane_ref("s", str(i)),
                                 collected_at=NOW).to_row() for i in range(40)]
    store.path.parent.mkdir(parents=True)
    store.path.write_text(json.dumps({"records": rows, "collections": []}), encoding="utf-8")
    kept = store.records()
    assert len(kept) == 10 and kept == rows[-10:] and store.records() == kept


def test_cr06_m02_incoming_batches_are_bounded_before_accumulation(tmp_path: Path) -> None:
    record = HerdrTelemetryRecord(machine_ref=MACHINE, session_ref=session_ref("s"), pane_ref=pane_ref("s", "p"),
                                  collected_at=NOW)
    endless = CollectResult(collected_at=NOW, outcome=HerdrCollectionOutcome.COLLECTED,
                            source_health=HerdrHealth.HEALTHY,
                            records=itertools.repeat(record))  # type: ignore[arg-type]
    store = _store(tmp_path, Clock())
    with pytest.raises(HerdrStoreError, match="HERDR_STORE_BATCH_TOO_LARGE"):
        store.record_collection(endless)
    assert not store.path.exists()
    wide = pane_doc(*({"pane_id": f"p-{i}", "state": "idle"} for i in range(MAX_ENTRIES * 4)))
    assert adapter_for("idle.json").translate(raw(wide)).error_code is HerdrDiagnostic.RAW_DOC_TOO_MANY_PANES


@POSIX_ONLY
def test_cr06_m02_fifo_in_place_of_the_store_never_blocks(tmp_path: Path) -> None:
    store = _store(tmp_path, Clock())
    store.path.parent.mkdir(parents=True)
    os.mkfifo(store.path)
    assert store.records() == [] and store.latest() == []


# --- M03: closed diagnostics ---------------------------------------------------------------------

class _HostileSource:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def installed(self) -> bool:
        return True

    def fetch(self) -> bytes:
        raise self.error


@pytest.mark.parametrize("error", [
    HerdrSourceError("\x1b[2J$ cat notes.txt HERDR-TERMINAL-MARKER"),
    RuntimeError("HERDR-TERMINAL-MARKER boom"),
    ValueError("HERDR-TERMINAL-MARKER"),
    OSError(5, "HERDR-TERMINAL-MARKER"),
])
def test_cr06_m03_unfamiliar_source_errors_become_source_error(tmp_path: Path, error: Exception) -> None:
    clock = Clock()
    result = HerdrAdapter(_HostileSource(error), machine_ref=MACHINE, ref_key=TEST_KEY, clock=clock).collect()
    assert result.error_code is HerdrDiagnostic.SOURCE_ERROR
    store = _store(tmp_path, clock)
    store.record_collection(result)
    assert "HERDR-TERMINAL-MARKER" not in _everything(store, result)
    assert store.latest_collection()["code"] == "HERDR_SOURCE_ERROR"


def test_cr06_m03_source_error_never_carries_free_text() -> None:
    error = HerdrSourceError("arbitrary terminal text")
    assert error.code is HerdrDiagnostic.SOURCE_ERROR and str(error) == "HERDR_SOURCE_ERROR"
    assert diagnostic("HERDR_SOURCE_TIMEOUT") is HerdrDiagnostic.SOURCE_TIMEOUT
    assert diagnostic(object()) is HerdrDiagnostic.SOURCE_ERROR


# --- M04: one deadline, process-group cleanup ------------------------------------------------------

def test_cr06_m04_waitid_path_is_non_reaping_and_uses_all_required_primitives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(adapter_module.os, "P_PID", 11, raising=False)
    monkeypatch.setattr(adapter_module.os, "WEXITED", 2, raising=False)
    monkeypatch.setattr(adapter_module.os, "WNOHANG", 4, raising=False)
    monkeypatch.setattr(adapter_module.os, "WNOWAIT", 8, raising=False)

    def fake_waitid(idtype: int, pid: int, options: int) -> SimpleNamespace:
        calls.append((idtype, pid, options))
        return SimpleNamespace(si_pid=pid)

    monkeypatch.setattr(adapter_module.os, "waitid", fake_waitid, raising=False)
    assert adapter_module._exited_unreaped(123, time.monotonic() + 1)
    assert calls == [(11, 123, 14)]


@pytest.mark.parametrize("missing", ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT"))
def test_cr06_m04_missing_waitid_primitive_fails_closed_with_source_unavailable(
    missing: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter_module.os, "waitid", lambda *args: None, raising=False)
    monkeypatch.setattr(adapter_module.os, "P_PID", 11, raising=False)
    monkeypatch.setattr(adapter_module.os, "WEXITED", 2, raising=False)
    monkeypatch.setattr(adapter_module.os, "WNOHANG", 4, raising=False)
    monkeypatch.setattr(adapter_module.os, "WNOWAIT", 8, raising=False)
    monkeypatch.setattr(adapter_module.os, missing, None, raising=False)
    assert adapter_module._waitid_primitives() is None
    with pytest.raises(HerdrSourceError) as caught:
        adapter_module._exited_unreaped(123, time.monotonic() + 1)
    assert caught.value.code is HerdrDiagnostic.SOURCE_UNAVAILABLE


@POSIX_ONLY
def test_cr06_m04_missing_waitid_fails_before_spawning(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not spawn without non-reaping waitid")

    monkeypatch.setattr(adapter_module, "_waitid_primitives", lambda: None)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    with pytest.raises(HerdrSourceError) as caught:
        bounded_run((sys.executable, "-c", "pass"), timeout_seconds=1, max_bytes=100)
    assert caught.value.code is HerdrDiagnostic.SOURCE_UNAVAILABLE

def _gone(pid: int, budget: float = 5.0) -> bool:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        status = Path(f"/proc/{pid}/stat")
        if status.exists():
            try:
                if status.read_text().split(")")[-1].split()[0] == "Z":
                    return True  # killed, awaiting reaping by its new parent
            except OSError:
                return True
        time.sleep(0.05)
    return False


def _spawner(pid_file: Path, *, after: str, grandchild_stdout: str = "None") -> str:
    return (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdout={grandchild_stdout})\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        f"{after}\n"
    )


@WAITID_ONLY
def test_cr06_m04_timeout_kills_descendants(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    script = _spawner(pid_file, after="time.sleep(60)")
    with pytest.raises(HerdrSourceError) as caught:
        bounded_run((sys.executable, "-c", script), timeout_seconds=1.5, max_bytes=100)
    assert caught.value.code is HerdrDiagnostic.SOURCE_TIMEOUT
    assert _gone(int(pid_file.read_text()))


@WAITID_ONLY
def test_cr06_m04_descendants_die_after_a_normal_exit_and_on_the_output_cap(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    script = _spawner(pid_file, after="print('{}')", grandchild_stdout="subprocess.DEVNULL")
    assert bounded_run((sys.executable, "-c", script), timeout_seconds=10, max_bytes=100).strip() == b"{}"
    assert _gone(int(pid_file.read_text()))
    capped = tmp_path / "capped.pid"
    flood = _spawner(capped, after="while True: sys.stdout.write('x' * 65536)", grandchild_stdout="subprocess.DEVNULL")
    assert len(bounded_run((sys.executable, "-c", flood), timeout_seconds=10, max_bytes=1000)) == 1001
    assert _gone(int(capped.read_text()))


@WAITID_ONLY
def test_cr06_m04_one_deadline_covers_reading_and_waiting() -> None:
    script = "import os, time\nos.close(1)\ntime.sleep(30)"  # EOF at once, then never exits
    started = time.monotonic()
    with pytest.raises(HerdrSourceError) as caught:
        bounded_run((sys.executable, "-c", script), timeout_seconds=1.5, max_bytes=100)
    assert caught.value.code is HerdrDiagnostic.SOURCE_TIMEOUT
    assert time.monotonic() - started < 1.5 + 1.0
    slow = "import sys, time\nsys.stdout.write('{'); sys.stdout.flush(); time.sleep(1.0)\ntime.sleep(30)"
    started = time.monotonic()
    with pytest.raises(HerdrSourceError):
        bounded_run((sys.executable, "-c", slow), timeout_seconds=1.5, max_bytes=100)
    assert time.monotonic() - started < 1.5 + 1.0


@WAITID_ONLY
def test_cr06_m04_bounded_run_caps_output_and_reports_exit_status() -> None:
    script = "import sys\nwhile True: sys.stdout.write('x' * 65536)"
    assert len(bounded_run((sys.executable, "-c", script), timeout_seconds=10, max_bytes=1000)) == 1001
    with pytest.raises(HerdrSourceError) as caught:
        bounded_run((sys.executable, "-c", "raise SystemExit(3)"), timeout_seconds=10, max_bytes=100)
    assert caught.value.code is HerdrDiagnostic.SOURCE_EXIT_NONZERO
    assert bounded_run((sys.executable, "-c", "print('{}')"), timeout_seconds=10, max_bytes=100).strip() == b"{}"


@WAITID_ONLY
def test_cr06_m04_child_gets_a_minimal_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADOS_DATABASE_URL", "postgresql://example.invalid/db")
    script = "import os, json; print(json.dumps(sorted(os.environ)))"
    names = json.loads(bounded_run((sys.executable, "-c", script), timeout_seconds=10, max_bytes=10_000))
    assert "PADOS_DATABASE_URL" not in names
    assert set(names) <= {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "XDG_RUNTIME_DIR", "TMPDIR", "TEMP", "TMP",
                          "LC_CTYPE", "__CF_USER_TEXT_ENCODING"}


def test_cr06_m04_adapter_has_no_unbounded_communicate_path() -> None:
    text = Path(adapter_module.__file__).read_text(encoding="utf-8")
    assert "communicate(" not in text and "start_new_session=True" in text and "os.killpg" in text


def test_cr06_states_have_no_live_projection_without_a_collection(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    store.path.parent.mkdir(parents=True)
    row = HerdrTelemetryRecord(machine_ref=MACHINE, session_ref=session_ref("s"), pane_ref=pane_ref("s", "p"),
                               state=HerdrAgentState.WORKING, collected_at=NOW,
                               source_observed_at=NOW - timedelta(seconds=1)).to_row()
    store.path.write_text(json.dumps({"records": [row], "collections": []}), encoding="utf-8")
    (projected,) = store.latest()
    assert projected["stale"] is True and projected["stale_reason"] == "NOT_COLLECTED"


def test_cr06_visibility_uses_the_real_clock_consistently(tmp_path: Path) -> None:
    path = write_config(tmp_path, fixture_path=str(fresh_fixture(tmp_path, "idle.json", datetime.now(UTC))))
    config = load_config(str(path))
    assert isinstance(config, HerdrConfig)
    config.collect()
    assert herdr_visibility(str(path))["agents_live"] == 1


# --- CR-06b N01: a pane missing from the newest snapshot is never live -------------------------------

def test_cr06b_n01_pane_absent_from_newer_snapshot_is_not_live(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    adapter = adapter_for("idle.json", clock)
    both = pane_doc({"pane_id": "p-a", "state": "working"}, {"pane_id": "p-b", "state": "idle"},
                    snapshot_at=NOW)
    store.record_collection(adapter.translate(raw(both)))
    assert {row["state"] for row in store.latest()} == {"WORKING", "IDLE"}
    clock.advance(seconds=10)
    only_b = pane_doc({"pane_id": "p-b", "state": "idle"}, snapshot_at=NOW + timedelta(seconds=10))
    store.record_collection(adapter.translate(raw(only_b)))
    rows = {row["last_observed_state"]: row for row in store.latest()}
    assert rows["WORKING"]["stale"] is True and rows["WORKING"]["state"] == "UNKNOWN"
    assert rows["WORKING"]["stale_reason"] == "NOT_IN_LATEST_SNAPSHOT"
    assert rows["IDLE"]["stale"] is False and rows["IDLE"]["state"] == "IDLE"
