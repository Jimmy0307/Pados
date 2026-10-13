"""Session guard: tests must never rewrite tracked authoritative or historical evidence."""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# Tracked evidence and history that no test may mutate. Source and test files
# are deliberately excluded so an in-progress edit never trips the guard.
GUARDED = ("runtime/artifacts", "outputs", "wiki", "RELEASE_MANIFEST.json", "config/release_manifest.json")


def tracked_evidence_digests(root: Path = ROOT) -> dict[str, str]:
    listed = subprocess.run(["git", "ls-files", "-z", "--", *GUARDED], cwd=root, capture_output=True, check=False)
    if listed.returncode != 0:
        return {}
    # A gate runner that writes its own tracked logs while pytest runs declares
    # that directory explicitly; nothing else is excluded.
    excluded = tuple(p for p in os.environ.get("PADOS_TEST_EVIDENCE_GUARD_EXCLUDE", "").split(os.pathsep) if p)
    digests: dict[str, str] = {}
    for name in filter(None, listed.stdout.decode().split("\0")):
        if excluded and name.startswith(excluded):
            continue
        path = root / name
        digests[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "<missing>"
    return digests


@pytest.fixture(scope="session", autouse=True)
def tracked_evidence_is_not_mutated():
    before = tracked_evidence_digests()
    yield
    after = tracked_evidence_digests()
    changed = sorted(name for name in before if after.get(name) != before[name])
    assert not changed, f"tests mutated tracked evidence: {changed[:20]}"
