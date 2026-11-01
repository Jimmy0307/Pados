# SPDX-License-Identifier: AGPL-3.0-only
"""RC2 INST-4: when one lane fails mid-migration, the report says which lanes moved and where every lane is now."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from pados.install import database


def _lane(name: str, state: str, current: str | None) -> dict[str, Any]:
    return {"lane": name, "database": f"pados_{name}", "state": state, "current_revision": current,
            "target_revision": "20261101_0020"}


def test_a_failed_lane_reports_the_partial_state_of_every_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    before = {"status": "UPGRADE_REQUIRED", "lanes": [_lane("operational", "UPGRADE_AVAILABLE", "20261010_0019"),
                                                      _lane("corpus", "UNINITIALIZED", None)]}
    after = {"status": "UPGRADE_REQUIRED", "lanes": [_lane("operational", "CURRENT", "20261101_0020"),
                                                     _lane("corpus", "UNINITIALIZED", None)]}
    checks = iter([before, after])
    monkeypatch.setattr(database, "check", lambda _settings: next(checks))
    exits = iter([0, 1])

    def run(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(returncode=next(exits), stderr="alembic: boom\n")

    monkeypatch.setattr(subprocess, "run", run)
    result = database.upgrade(SimpleNamespace(), root=None)  # type: ignore[arg-type]
    assert result["status"] == "FAILED" and result["partial"] is True
    assert [(a["lane"], a["exit_code"]) for a in result["applied"]] == [("operational", 0), ("corpus", 1)]
    assert {lane["lane"]: lane["state"] for lane in result["after"]["lanes"]} == {
        "operational": "CURRENT", "corpus": "UNINITIALIZED"}
