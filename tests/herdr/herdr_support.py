"""Shared deterministic helpers for Herdr tests (no network, no Herdr executable)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pados.herdr.adapter import FixtureHerdrSource, HerdrAdapter, HerdrBinding, keyed_ref

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "herdr"
MACHINE = "mach-" + "0123456789abcdef0123"
OTHER_MACHINE = "mach-" + "f" * 20
NOW = datetime(2026, 10, 2, 10, 0, tzinfo=UTC)
FIXTURE_SNAPSHOT = datetime(2026, 10, 2, 9, 59, 30, tzinfo=UTC)
TERMINAL_MARKER = "HERDR-TERMINAL-MARKER"
LABEL_MARKER = "HERDR-LABEL-MARKER"
TEST_KEY = bytes(range(32))


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now = self.now + timedelta(**delta)


def fixture(name: str) -> Path:
    return FIXTURES / name


def adapter_for(name: str, clock: Clock | None = None, machine_ref: str = MACHINE, *,
                bindings: dict[str, HerdrBinding] | None = None, key: bytes = TEST_KEY,
                stale_after_seconds: int = 120) -> HerdrAdapter:
    return HerdrAdapter(FixtureHerdrSource(fixture(name)), machine_ref=machine_ref, ref_key=key,
                        bindings=bindings, stale_after_seconds=stale_after_seconds, clock=clock or Clock())


def session_ref(session: str, key: bytes = TEST_KEY) -> str:
    return keyed_ref(key, "hs", session)


def pane_ref(session: str, pane: str, key: bytes = TEST_KEY) -> str:
    return keyed_ref(key, "hp", session, pane)


def raw(document: Any) -> bytes:
    return json.dumps(document).encode("utf-8")


def pane_doc(*panes: dict[str, Any], session_id: str = "s-alpha",
             snapshot_at: datetime | None = FIXTURE_SNAPSHOT) -> dict[str, Any]:
    document: dict[str, Any] = {"sessions": [{"session_id": session_id, "panes": list(panes)}]}
    if snapshot_at is not None:
        document["snapshot_at"] = snapshot_at.isoformat()
    return document


def fresh_fixture(tmp_path: Path, name: str, snapshot_at: datetime | None = None) -> Path:
    """A copy of a fixture whose document-level source time is ``snapshot_at`` (default: now)."""
    document = json.loads(fixture(name).read_text(encoding="utf-8"))
    document["snapshot_at"] = (snapshot_at or datetime.now(UTC)).isoformat()
    path = tmp_path / f"fresh-{name}"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def write_config(tmp_path: Path, **overrides: Any) -> Path:
    config = {"enabled": True, "source": "fixture", "fixture_path": str(fixture("working.json")),
              "store_path": str(tmp_path / "herdr" / "telemetry.json"), "machine_ref": MACHINE,
              "stale_after_seconds": 120, "max_records": 100}
    config.update(overrides)
    path = tmp_path / "herdr-config.json"
    path.write_text(json.dumps({k: v for k, v in config.items() if v is not None}), encoding="utf-8")
    return path
