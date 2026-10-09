import zipfile
from pathlib import Path

import pytest

from pados.release.package import _scan_secret, build_release


INTERNAL_RELEASE_MANIFEST = Path(__file__).resolve().parents[2] / "config" / "release_manifest.json"
needs_release_manifest = pytest.mark.skipif(
    not INTERNAL_RELEASE_MANIFEST.is_file(),
    reason="config/release_manifest.json is an internal release manifest, not part of the public export")


@needs_release_manifest
def test_release_package_preserves_tree_and_excludes_runtime_content(tmp_path: Path) -> None:
    root = Path.cwd()
    result = build_release(root, output_path=tmp_path / "outputs" / "pados_release_p6_6_20260710.zip",
                           reports_root=tmp_path / "reports")
    assert result.sha256
    with zipfile.ZipFile(result.zip_path) as archive:
        names = archive.namelist()
    assert "src/pados/cli/main.py" in names
    assert "pyproject.toml" in names
    assert "web/package-lock.json" in names
    assert "web/src/main.tsx" in names
    assert "tests/fixtures/sample_repo/secret_sample.txt" not in names
    assert not any(name.endswith((".pyc", ".pyo", ".log")) for name in names)
    assert not any(name.startswith(("runtime/", "work/", "outputs/", ".git/", ".venv/")) for name in names)
    assert not any("/node_modules/" in name or name.startswith("node_modules/") for name in names)
    assert not any(name.startswith(("web/dist/", "web/build/", "web/coverage/")) for name in names)
    assert not any(name.endswith(".tsbuildinfo") for name in names)


def test_release_secret_scan_distinguishes_source_reference_from_literal(tmp_path: Path) -> None:
    source_reference = tmp_path / "source_reference.py"
    source_reference.write_text("connect(" + "password" + "=settings.db_password)\n", encoding="utf-8")
    literal = tmp_path / "literal.env"
    literal.write_text("API" + "_KEY" + "=example-secret-value\n", encoding="utf-8")
    bare_assignment = tmp_path / "bare_assignment.env"
    bare_assignment.write_text("pass" + "word" + "=abc\n", encoding="utf-8")

    assert _scan_secret(source_reference) is False
    assert _scan_secret(literal) is True
    assert _scan_secret(bare_assignment) is True
