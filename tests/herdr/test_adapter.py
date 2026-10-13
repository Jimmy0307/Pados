"""HerdrAdapter: bounded translation, terminal content dropped, unknown -> UNKNOWN, no execution."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from pados.herdr import adapter as adapter_module
from pados.herdr.adapter import (
    ALLOWED_COMMAND_ARGS,
    HERDR_STATUS_ARGS,
    MAX_ENTRIES,
    MAX_RAW_BYTES,
    MAX_RAW_DEPTH,
    MAX_SESSIONS,
    CommandHerdrSource,
    FixtureHerdrSource,
    HerdrAdapter,
    HerdrSourceConfigError,
    HerdrSourceError,
)
from pados.herdr.contract import HerdrAgentKind, HerdrAgentState, HerdrHealth
from pados.herdr.diagnostics import HerdrCollectionOutcome, HerdrDiagnostic
from tests.herdr.herdr_support import (
    FIXTURE_SNAPSHOT,
    MACHINE,
    NOW,
    TERMINAL_MARKER,
    TEST_KEY,
    Clock,
    adapter_for,
    fixture,
    pane_doc,
    pane_ref,
    raw,
    session_ref,
)


def _states(name: str) -> dict[str, HerdrAgentState]:
    return {r.pane_ref: r.state for r in adapter_for(name).collect().records}


@pytest.mark.parametrize(("name", "state"), [
    ("working.json", HerdrAgentState.WORKING), ("idle.json", HerdrAgentState.IDLE),
    ("blocked.json", HerdrAgentState.BLOCKED), ("waiting_input.json", HerdrAgentState.WAITING_INPUT),
    ("exited.json", HerdrAgentState.EXITED),
])
def test_each_state_fixture_translates_to_one_telemetry_record(name: str, state: HerdrAgentState) -> None:
    result = adapter_for(name).collect()
    assert result.outcome is HerdrCollectionOutcome.COLLECTED and result.error_code is None
    (record,) = result.records
    assert record.state is state and record.machine_ref == MACHINE
    assert (record.session_ref, record.pane_ref) == (session_ref("s-alpha"), pane_ref("s-alpha", "p-1"))
    assert record.collected_at == NOW and record.source_observed_at == FIXTURE_SNAPSHOT
    assert record.semantic_authority == "NONE"
    assert record.last_transition_at is not None and record.last_transition_at < NOW


def test_health_and_agent_kind_come_from_closed_vocabularies() -> None:
    (working,) = adapter_for("working.json").collect().records
    assert working.agent_kind is HerdrAgentKind.CLAUDE_CODE
    (blocked,) = adapter_for("blocked.json").collect().records
    assert blocked.agent_kind is HerdrAgentKind.CODEX
    (exited,) = adapter_for("exited.json").collect().records
    assert exited.health is HerdrHealth.DEGRADED


def test_unknown_raw_states_become_unknown() -> None:
    states = _states("unknown_state.json")
    assert len(states) == 3 and set(states.values()) == {HerdrAgentState.UNKNOWN}
    long_state = pane_doc({"pane_id": "p-1", "state": "working" + " " * 40})
    (record,) = adapter_for("idle.json").translate(raw(long_state)).records
    assert record.state is HerdrAgentState.UNKNOWN


def test_multi_session_documents_and_raw_key_aliases() -> None:
    result = adapter_for("multi_session.json").collect()
    by_key = {r.pane_ref: r for r in result.records}
    beta = by_key[pane_ref("s-beta", "p-9")]
    assert beta.state is HerdrAgentState.BLOCKED and beta.agent_kind is HerdrAgentKind.CODEX
    assert by_key[pane_ref("s-alpha", "p-2")].agent_kind is HerdrAgentKind.SHELL
    assert len(result.records) == 4 and result.entries_refused == 0


def test_terminal_content_fields_are_dropped_counted_and_never_copied() -> None:
    result = adapter_for("terminal_content.json").collect()
    # scrollback, pane_buffer, output, lines, history, screen, content, agent.last_output, terminal_history
    assert result.terminal_content_dropped == 9
    (record,) = result.records
    assert record.state is HerdrAgentState.WORKING
    serialized = json.dumps([r.to_row() for r in result.records]) + json.dumps(result.summary())
    assert TERMINAL_MARKER not in serialized


def test_terminal_content_inside_unknown_nesting_is_counted_but_never_inspected() -> None:
    document = pane_doc({"pane_id": "p-1", "state": "idle", "extras": {"ScreenDump": {"Buffer": "x"}}})
    result = adapter_for("idle.json").translate(raw(document))
    assert result.terminal_content_dropped == 1 and len(result.records) == 1


def test_oversized_document_is_refused_whole() -> None:
    assert fixture("oversized.json").stat().st_size > MAX_RAW_BYTES
    result = adapter_for("oversized.json").collect()
    assert result.records == () and result.error_code is HerdrDiagnostic.RAW_DOC_TOO_LARGE
    assert result.outcome is HerdrCollectionOutcome.DOCUMENT_REFUSED
    assert result.source_health is HerdrHealth.DEGRADED


def test_fixture_source_reads_at_most_cap_plus_one_byte() -> None:
    assert len(FixtureHerdrSource(fixture("oversized.json")).fetch()) == MAX_RAW_BYTES + 1


@pytest.mark.parametrize(("payload", "code"), [
    (b"{not json", HerdrDiagnostic.RAW_DOC_INVALID_JSON),
    (b"\xff\xfe", HerdrDiagnostic.RAW_DOC_INVALID_JSON),
    (b"[1, 2]", HerdrDiagnostic.RAW_DOC_SHAPE_INVALID),
    (b'{"sessions": {"a": 1}}', HerdrDiagnostic.RAW_DOC_SHAPE_INVALID),
])
def test_malformed_documents_are_refused(payload: bytes, code: HerdrDiagnostic) -> None:
    result = adapter_for("idle.json").translate(payload)
    assert result.records == () and result.error_code is code


def test_over_deep_and_over_wide_documents_are_refused() -> None:
    nested: dict[str, Any] = {"leaf": 1}
    for _ in range(MAX_RAW_DEPTH + 2):
        nested = {"n": nested}
    deep = pane_doc({"pane_id": "p-1", "state": "idle", "extra": nested})
    assert adapter_for("idle.json").translate(raw(deep)).error_code is HerdrDiagnostic.RAW_DOC_TOO_DEEP
    sessions = {"snapshot_at": NOW.isoformat(),
                "sessions": [{"session_id": f"s-{i}", "panes": []} for i in range(MAX_SESSIONS + 1)]}
    assert adapter_for("idle.json").translate(raw(sessions)).error_code is HerdrDiagnostic.RAW_DOC_TOO_MANY_SESSIONS
    wide = pane_doc(*({"pane_id": f"p-{i}", "state": "idle"} for i in range(MAX_ENTRIES + 1)))
    assert adapter_for("idle.json").translate(raw(wide)).error_code is HerdrDiagnostic.RAW_DOC_TOO_MANY_PANES


def test_invalid_entries_are_refused_individually_and_counted() -> None:
    document = pane_doc(
        {"pane_id": "p-1", "state": "idle"},
        {"pane_id": "x" * 300, "state": "idle"},
        {"pane_id": ["list"], "state": "idle"},
        {"pane_id": "p-1", "state": "working"},
        "not-a-pane",
    )
    document["sessions"] += [{"panes": "nope"}, {"panes": [{"pane_id": "p-2"}]}]
    result = adapter_for("idle.json").translate(raw(document))
    assert [r.pane_ref for r in result.records] == [pane_ref("s-alpha", "p-1")]
    assert result.records[0].state is HerdrAgentState.IDLE  # first wins; the duplicate is refused
    assert result.entries_refused == 6 and result.outcome is HerdrCollectionOutcome.COLLECTED_PARTIAL
    assert set(result.refusal_codes) == {HerdrDiagnostic.REF_INVALID, HerdrDiagnostic.PANE_DUPLICATE,
                                         HerdrDiagnostic.PANE_SHAPE_INVALID, HerdrDiagnostic.SESSION_SHAPE_INVALID}


def test_unsafe_transition_times_are_dropped_not_copied() -> None:
    document = pane_doc(
        {"pane_id": "p-1", "state": "idle", "last_transition_at": "2030-01-01T00:00:00+00:00"},
        {"pane_id": "p-2", "state": "idle", "last_transition_at": "2026-10-02T09:00:00"},
    )
    first, second = adapter_for("idle.json").translate(raw(document)).records
    assert first.last_transition_at is None and second.last_transition_at is None


def test_machine_ref_is_bound_by_pados_and_must_be_a_machine_id() -> None:
    (record,) = adapter_for("authority_claim.json").collect().records
    assert record.machine_ref == MACHINE
    with pytest.raises(HerdrSourceConfigError):
        HerdrAdapter(FixtureHerdrSource(fixture("idle.json")), machine_ref="workstation-01", ref_key=TEST_KEY)
    with pytest.raises(HerdrSourceConfigError, match="HERDR_REF_KEY_INVALID"):
        HerdrAdapter(FixtureHerdrSource(fixture("idle.json")), machine_ref=MACHINE, ref_key=b"short")


def test_unavailable_source_is_unreachable_with_no_records(tmp_path: Path) -> None:
    result = HerdrAdapter(FixtureHerdrSource(tmp_path / "missing.json"), machine_ref=MACHINE, ref_key=TEST_KEY,
                          clock=Clock()).collect()
    assert result.source_health is HerdrHealth.UNREACHABLE and result.records == ()
    assert result.outcome is HerdrCollectionOutcome.SOURCE_UNREACHABLE
    assert result.error_code is HerdrDiagnostic.FIXTURE_UNAVAILABLE


def test_collect_starts_no_thread_and_translate_is_deterministic() -> None:
    before = threading.active_count()
    adapter = adapter_for("multi_session.json")
    first, second = adapter.collect(), adapter.collect()
    assert threading.active_count() == before
    assert [r.to_row() for r in first.records] == [r.to_row() for r in second.records]
    assert first.document_digest == second.document_digest is not None


# --- CommandHerdrSource: defined, never invoked ---------------------------------------------------

class _NoExecution:
    calls = 0

    def __call__(self, *args: object, **kwargs: object) -> None:
        type(self).calls += 1
        raise AssertionError("a test must never execute Herdr")


def test_command_source_has_a_fixed_allowlisted_argv_and_bounds() -> None:
    assert ALLOWED_COMMAND_ARGS == frozenset({HERDR_STATUS_ARGS}) and HERDR_STATUS_ARGS == ("status", "--json")
    for args in (("status", "--json", "--all"), ("run", "rm"), ("status;id",), ()):
        with pytest.raises(HerdrSourceConfigError, match="HERDR_COMMAND_NOT_ALLOWLISTED"):
            CommandHerdrSource(args)
    for timeout in (0, 31, True):
        with pytest.raises(HerdrSourceConfigError, match="HERDR_COMMAND_TIMEOUT_INVALID"):
            CommandHerdrSource(timeout_seconds=timeout)
    with pytest.raises(HerdrSourceConfigError, match="HERDR_COMMAND_MAX_BYTES_INVALID"):
        CommandHerdrSource(max_bytes=MAX_RAW_BYTES + 1)


@pytest.mark.skipif(sys.platform == "win32", reason="the command source is disabled on Windows")
def test_command_source_presence_and_missing_install_never_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _NoExecution()
    monkeypatch.setattr(subprocess, "Popen", guard)
    monkeypatch.setattr(adapter_module, "bounded_run", guard)
    assert CommandHerdrSource(which=lambda name: None).installed() is False
    assert CommandHerdrSource(which=lambda name: "/opt/herdr/bin/herdr").installed() is True
    with pytest.raises(HerdrSourceError) as caught:
        CommandHerdrSource(which=lambda name: None).fetch()
    assert caught.value.code is HerdrDiagnostic.NOT_INSTALLED
    result = HerdrAdapter(CommandHerdrSource(which=lambda name: None), machine_ref=MACHINE,
                          ref_key=TEST_KEY).collect()
    assert result.source_health is HerdrHealth.UNREACHABLE and result.error_code is HerdrDiagnostic.NOT_INSTALLED
    assert _NoExecution.calls == 0


@pytest.mark.skipif(sys.platform == "win32", reason="the command source is disabled on Windows")
def test_command_source_passes_the_fixed_argv_without_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[tuple[str, ...], float, int]] = []

    def fake_run(argv: tuple[str, ...], *, timeout_seconds: float, max_bytes: int) -> bytes:
        seen.append((tuple(argv), timeout_seconds, max_bytes))
        return raw(pane_doc({"pane_id": "p-1", "state": "working"}))

    monkeypatch.setattr(adapter_module, "bounded_run", fake_run)
    source = CommandHerdrSource(timeout_seconds=2, which=lambda name: f"/opt/{name}/bin/{name}")
    (record,) = HerdrAdapter(source, machine_ref=MACHINE, ref_key=TEST_KEY, clock=Clock()).collect().records
    assert record.state is HerdrAgentState.WORKING
    assert seen == [(("/opt/herdr/bin/herdr", "status", "--json"), 2.0, MAX_RAW_BYTES)]
