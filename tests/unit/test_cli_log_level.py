# SPDX-License-Identifier: AGPL-3.0-only
"""Usability: an unknown ``PADOS_LOG_LEVEL`` never stops the diagnostic and repair commands.

Found in review: ``getattr(logging, level.upper())`` raised ``AttributeError`` for a value such as ``bogus``, so every
command, ``pados --help``, ``pados init`` and ``pados doctor`` included, crashed before it ran. Logging now falls back to
INFO, the problem is written once to standard error, and ``doctor`` reports it as data.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pados.logging_config import configure_logging, resolve_log_level

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(("value", "level"), [("DEBUG", logging.DEBUG), ("info", logging.INFO), (" Warning ", logging.WARNING),
                                              ("error", logging.ERROR), ("CRITICAL", logging.CRITICAL), ("", logging.INFO),
                                              (None, logging.INFO)])
def test_known_or_blank_levels_resolve_without_a_problem(value: object, level: int) -> None:
    assert resolve_log_level(value) == (level, None)


@pytest.mark.parametrize("value", ["bogus", "5", "basicConfig", "NOTSET", "info\x1b[31m"])
def test_unknown_levels_fall_back_to_info_with_a_stable_problem(value: str) -> None:
    numeric, problem = resolve_log_level(value)
    assert numeric == logging.INFO and problem is not None and problem.startswith("PADOS_LOG_LEVEL_INVALID: ")
    assert "\x1b" not in problem  # the echoed value is cut and printable only


def test_configure_logging_does_not_raise_and_reports_once(capsys: pytest.CaptureFixture[str]) -> None:
    assert configure_logging("bogus", to_stderr=True) is not None
    assert capsys.readouterr().err.count("PADOS_LOG_LEVEL_INVALID") == 1
    assert configure_logging("INFO", to_stderr=True) is None


def _cli(*args: str, cwd: Path, level: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PADOS_LOG_LEVEL": level, "PADOS_DB_HOST": "127.0.0.1", "PADOS_DB_PORT": "1",
           "PADOS_DB_PASSWORD": "unused-local", "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run([sys.executable, "-c", "from pados.cli.main import app; app()", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120, check=False)


def test_init_runs_with_an_unknown_level(tmp_path: Path) -> None:
    done = _cli("init", "--dir", str(tmp_path), cwd=ROOT, level="bogus")
    assert done.returncode == 0, done.stderr
    assert "Traceback" not in done.stderr and done.stderr.count("PADOS_LOG_LEVEL_INVALID") == 1
    assert (tmp_path / ".env").is_file()


def test_doctor_reports_an_unknown_level_as_data() -> None:
    done = _cli("doctor", "--json", cwd=ROOT, level="bogus")
    assert "Traceback" not in done.stderr and done.stderr.count("PADOS_LOG_LEVEL_INVALID") == 1
    report = json.loads(done.stdout)  # still one JSON document
    assert report["settings"]["log_level"]["status"] == "invalid"
    assert report["settings"]["log_level"]["problem"].startswith("PADOS_LOG_LEVEL_INVALID")


def test_doctor_outside_the_checkout_says_why(tmp_path: Path) -> None:
    done = _cli("doctor", "--json", cwd=tmp_path, level="INFO")
    assert done.returncode == 2 and "WORKTREE_IMPORT_PATH_MISMATCH" in done.stderr
    assert "Unexpected internal error" not in done.stderr
