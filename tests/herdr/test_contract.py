"""Contract ``pados.herdr.telemetry.v1``: frozen, closed, bounded, no source-controlled free text."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from pados.herdr.adapter import is_terminal_content_key
from pados.herdr.contract import (
    AGENT_KIND_MAP,
    RECORD_KEYS,
    SCHEMA_ID,
    HerdrAgentKind,
    HerdrAgentState,
    HerdrHealth,
    HerdrTelemetryRecord,
    parse_aware,
)
from pados.lifecycle.minimization import prohibited_field
from tests.herdr.herdr_support import MACHINE, NOW, pane_ref, session_ref


def _record(**overrides: object) -> HerdrTelemetryRecord:
    values: dict[str, object] = {"machine_ref": MACHINE, "session_ref": session_ref("s"),
                                 "pane_ref": pane_ref("s", "p"), "collected_at": NOW}
    values.update(overrides)
    return HerdrTelemetryRecord.model_validate(values)


def test_schema_id_and_enumerations_are_the_documented_contract() -> None:
    assert SCHEMA_ID == "pados.herdr.telemetry.v1"
    assert [s.value for s in HerdrAgentState] == ["WORKING", "IDLE", "BLOCKED", "WAITING_INPUT", "EXITED", "UNKNOWN"]
    assert [h.value for h in HerdrHealth] == ["HEALTHY", "DEGRADED", "UNREACHABLE"]
    assert set(AGENT_KIND_MAP.values()) <= set(HerdrAgentKind)


def test_record_row_is_exactly_the_telemetry_schema_with_no_authority() -> None:
    row = _record().to_row()
    assert tuple(row) == RECORD_KEYS
    assert row["schema_id"] == SCHEMA_ID and row["semantic_authority"] == "NONE"
    assert row["state"] == "UNKNOWN" and row["agent_kind"] == "UNKNOWN" and row["source_observed_at"] is None


def test_schema_has_no_field_for_terminal_content_labels_or_prohibited_data() -> None:
    for key in RECORD_KEYS:
        assert not is_terminal_content_key(key), key
        assert prohibited_field(key) is None, key
        assert "label" not in key and "name" not in key


def test_record_is_frozen_and_closed() -> None:
    record = _record()
    with pytest.raises(ValidationError):
        record.state = HerdrAgentState.WORKING  # type: ignore[misc]
    for extra in ({"scrollback": "x"}, {"pados_status": "ACCEPTED"}, {"hostname": "box"}, {"label": "x"}):
        with pytest.raises(ValidationError):
            _record(**extra)


@pytest.mark.parametrize("value", ["workstation-01", "mach-0123", "MACH-0123456789ABCDEF0123",
                                   "mach-0123456789abcdef01234", "mach-0123456789abcdef012g", ""])
def test_machine_ref_is_a_pados_machine_id_never_a_hostname(value: str) -> None:
    with pytest.raises(ValidationError):
        _record(machine_ref=value)


@pytest.mark.parametrize(("field", "value"), [
    ("session_ref", "s-alpha"), ("session_ref", "hs-0123"), ("session_ref", "hp-0123456789abcdef"),
    ("pane_ref", "p-1"), ("pane_ref", "hs-0123456789abcdef"), ("pane_ref", "hp-0123456789ABCDEF"),
    ("project_ref", "has space"), ("project_ref", "x" * 81), ("work_package_ref", "line\nbreak"),
])
def test_refs_are_pados_digests_or_operator_ids(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _record(**{field: value})


def test_agent_kind_is_a_closed_vocabulary() -> None:
    assert _record(agent_kind="CODEX").agent_kind is HerdrAgentKind.CODEX
    with pytest.raises(ValidationError):
        _record(agent_kind="Claude Code")


def test_times_are_aware_and_never_after_collection() -> None:
    with pytest.raises(ValidationError):
        _record(collected_at=datetime(2026, 10, 2, 10, 0))
    with pytest.raises(ValidationError):
        _record(source_observed_at=NOW + timedelta(minutes=5))
    with pytest.raises(ValidationError):
        _record(last_transition_at=NOW + timedelta(minutes=5))
    assert _record(source_observed_at=NOW - timedelta(seconds=5)).source_observed_at is not None


def test_parse_aware_rejects_untrustworthy_timestamps() -> None:
    assert parse_aware("2026-10-02T10:00:00+00:00") == NOW
    for value in (None, 5, "", "not-a-time", "2026-10-02T10:00:00", "9" * 65):
        assert parse_aware(value) is None
