# SPDX-License-Identifier: AGPL-3.0-only
"""Usability: a CLI command's ``--json`` standard output is exactly one JSON document.

Structured log events go to standard error, so ``pados doctor --json | jq`` and scripts that parse the output work.
Found in the local usability walkthrough: ``doctor`` printed its ``doctor_completed`` log event after the JSON.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_doctor_json_stdout_is_one_document_and_logs_go_to_stderr() -> None:
    env = {**os.environ, "PADOS_DB_HOST": "127.0.0.1", "PADOS_DB_PORT": "1", "PADOS_DB_PASSWORD": "unused-local"}
    done = subprocess.run([sys.executable, "-c", "from pados.cli.main import app; app()", "doctor", "--json"],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False)
    document = json.loads(done.stdout)  # raises on a trailing log line
    assert isinstance(document, dict) and "status" in document
    assert '"event": "doctor_completed"' in done.stderr
