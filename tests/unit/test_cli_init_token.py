# SPDX-License-Identifier: AGPL-3.0-only
"""`pados init` writes a unique control token once; a repeated init never silently replaces it.

The token is what `pados serve` (and the Windows web launcher, which now starts the backend with `pados serve`)
checks before it listens: the shipped default hash is refused, so an install must run `pados init` first.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _init(directory: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("PADOS_")}
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, "-c", "from pados.cli.main import app; app()", "init", "--dir",
                           str(directory), *extra], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
                          check=False)


def _hash(directory: Path) -> str:
    match = re.search(r"^PADOS_CONTROL_API_TOKEN_HASH=([0-9A-F]{64})$", (directory / ".env").read_text("utf-8"), re.M)
    assert match is not None
    return match.group(1)


def test_a_repeated_init_keeps_the_token_and_force_makes_a_new_one(tmp_path: Path) -> None:
    from pados.security_posture import DEFAULT_CONTROL_TOKEN_HASH

    assert _init(tmp_path).returncode == 0
    first = _hash(tmp_path)
    assert first != DEFAULT_CONTROL_TOKEN_HASH
    again = _init(tmp_path)
    assert again.returncode == 1 and "--force" in again.stdout
    assert _hash(tmp_path) == first  # unchanged: a running install keeps working
    assert _init(tmp_path, "--force").returncode == 0
    assert _hash(tmp_path) not in {first, DEFAULT_CONTROL_TOKEN_HASH}


def test_two_installs_never_share_a_token(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    assert _init(one).returncode == 0 and _init(two).returncode == 0
    assert _hash(one) != _hash(two)
