# SPDX-License-Identifier: AGPL-3.0-only
"""The live M4.1 dogfood measures only the M1-pinned artifact, whatever M3 report the operator names.

Review of the re-measurement parameters: ``--m3-report`` with ``--expected-profile-id`` alone is circular (an operator
can name any report and its own id). The report's profile must name exactly the pinned artifact (reference, manifest
digest and layer digests, the same constants the M2 benchmark refuses to measure anything else with), and overriding
the report requires naming the expected id explicitly.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "stage_b"


def _module(name: str) -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _profile(**artifact: object) -> SimpleNamespace:
    bench = _module("windows_m2_benchmark")
    values = {"artifact_ref": bench.APPROVED_MODEL, "manifest_digest": bench.APPROVED_MANIFEST_DIGEST,
              "layer_digests": list(bench.APPROVED_LAYERS)}
    values.update(artifact)
    return SimpleNamespace(artifact=SimpleNamespace(**values))


def test_a_profile_naming_the_pinned_artifact_is_accepted() -> None:
    _module("windows_m4_1_dogfood").assert_pinned_artifact(_profile())


@pytest.mark.parametrize("artifact", [
    {"artifact_ref": "qwen3:14b"},
    {"manifest_digest": "sha256:" + "0" * 64},
    {"layer_digests": ["sha256:" + "1" * 64]},
])
def test_a_profile_for_any_other_artifact_is_refused(artifact: dict[str, object]) -> None:
    with pytest.raises(RuntimeError, match="M3_PROFILE_ARTIFACT_NOT_PINNED"):
        _module("windows_m4_1_dogfood").assert_pinned_artifact(_profile(**artifact))


def test_overriding_the_m3_report_requires_an_explicit_expected_profile_id(tmp_path: Path) -> None:
    run = subprocess.run([sys.executable, str(SCRIPTS / "windows_m4_1_dogfood.py"), "--expected-commit", "0" * 40,
                          "--expected-tree", "0" * 40, "--m3-report", str(tmp_path / "report.json"),
                          "--out", str(tmp_path / "out")], capture_output=True, text=True, check=False, timeout=120)
    assert run.returncode == 2
    assert "REFUSED: --m3-report needs --expected-profile-id" in run.stdout
    assert not (tmp_path / "out").exists()
