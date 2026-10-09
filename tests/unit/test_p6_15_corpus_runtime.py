from __future__ import annotations

import json
from pathlib import Path
from typing import Self

import pytest

from pados.corpus import runtime
from pados.corpus.security import CorpusBoundaryError, discover_pdf_files, resolve_child_path


@pytest.fixture(autouse=True)
def reset_runtime_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "AUTHORIZATION_FILE", None)
    monkeypatch.setattr(runtime, "APPROVED_ROOT", None)
    monkeypatch.setattr(runtime, "RUNTIME_ARTIFACT_ROOT", runtime.PROJECT_ROOT / "runtime" / "artifacts" / "p6_15_corpus_runtime")
    monkeypatch.setattr(runtime, "ISOLATED_DATABASE", "")
    monkeypatch.setattr(runtime, "OPERATIONAL_DATABASE", "")
    monkeypatch.setattr(runtime, "PROJECT_ID", None)
    monkeypatch.setattr(runtime, "_ACTIVE_CONFIGURATION", None)


def _authorization(root: Path) -> dict[str, object]:
    return {
        "authorization_status": "AUTHORIZED",
        "project_id": "00000000-0000-0000-0000-000000000015",
        "approved_corpus_root": str(root.resolve()),
        "database": "p6_15_test_db",
        "operational_database": "pados_local_runtime",
        "embedding_policy": runtime.POLICY,
        "allow_ocr": False,
        "allow_cloud_models": False,
        "allow_source_modification": False,
        "allow_operational_migration": False,
        "maximum_documents": runtime.MAX_DOCUMENTS,
    }


def _scan_manifest(root: Path, path: Path) -> dict[str, object]:
    return {
        "manifest_type": "P6.15_CONTROLLED_RUNTIME_SCAN",
        "status": "PASS",
        "approved_root": str(root.resolve()),
        "documents": [
            {
                "relative_path": path.name,
                "canonical_absolute_path": str(path.resolve()),
                "file_size": path.stat().st_size,
                "modified_timestamp": "2026-07-31T00:00:00+00:00",
                "sha256": runtime._sha256(path),
                "readability": "READABLE",
                "known_status": "UNKNOWN",
                "change_status": "NEW",
                "duplicate_group": None,
            }
        ],
    }


def test_authorization_accepts_utf8_bom_and_requires_exact_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    auth_path = tmp_path / "auth.json"
    auth_path.write_bytes(b"\xef\xbb\xbf" + json.dumps(_authorization(root)).encode())
    monkeypatch.setattr(runtime, "AUTHORIZATION_FILE", auth_path)
    monkeypatch.setattr(runtime, "APPROVED_ROOT", root.resolve())

    assert runtime._load_authorization()["project_id"] == "00000000-0000-0000-0000-000000000015"


def test_review_authorization_binds_manifest_and_file_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    pdf = root / "document.pdf"
    pdf.write_bytes(b"fixture pdf bytes")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps(_authorization(root)), encoding="utf-8")
    scan_path = tmp_path / "scan.json"
    scan_path.write_text(json.dumps(_scan_manifest(root, pdf)), encoding="utf-8")
    monkeypatch.setattr(runtime, "AUTHORIZATION_FILE", auth_path)
    monkeypatch.setattr(runtime, "APPROVED_ROOT", root.resolve())
    monkeypatch.setattr(runtime, "RUNTIME_ARTIFACT_ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(runtime, "_doctor_or_raise", lambda: {"provenance": {}})

    reviewed = runtime.review(scan_manifest=scan_path, selected=["document.pdf"])
    manifest_path = Path(str(reviewed["manifest_path"]))
    authorized_path = tmp_path / "authorization.json"
    runtime.authorize_batch(
        manifest_path=manifest_path,
        output=authorized_path,
        authorizer="test-human",
        confirm=True,
    )
    manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(runtime.CorpusRuntimeBlocked, match="MANIFEST_HASH_MISMATCH"):
        runtime._verify_authorization(manifest_path, authorized_path)


def test_authorization_rejects_changed_file_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    pdf = root / "document.pdf"
    pdf.write_bytes(b"fixture pdf bytes")
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps(_authorization(root)), encoding="utf-8")
    scan_path = tmp_path / "scan.json"
    scan_path.write_text(json.dumps(_scan_manifest(root, pdf)), encoding="utf-8")
    monkeypatch.setattr(runtime, "AUTHORIZATION_FILE", auth_path)
    monkeypatch.setattr(runtime, "APPROVED_ROOT", root.resolve())
    monkeypatch.setattr(runtime, "RUNTIME_ARTIFACT_ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(runtime, "_doctor_or_raise", lambda: {"provenance": {}})

    manifest_path = Path(str(runtime.review(scan_manifest=scan_path)["manifest_path"]))
    authorization_path = tmp_path / "authorization.json"
    runtime.authorize_batch(
        manifest_path=manifest_path,
        output=authorization_path,
        authorizer="test-human",
        confirm=True,
    )
    pdf.write_bytes(b"changed fixture pdf bytes")

    with pytest.raises(runtime.CorpusRuntimeBlocked, match="FILE_HASH_CHANGED"):
        runtime._verify_authorization(manifest_path, authorization_path)


def test_root_boundary_and_ceiling_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    with pytest.raises(CorpusBoundaryError):
        resolve_child_path(root, "../outside.pdf")
    for index in range(runtime.MAX_DOCUMENTS + 1):
        (root / f"{index}.pdf").write_bytes(b"pdf")
    with pytest.raises(CorpusBoundaryError):
        discover_pdf_files(root, max_documents=runtime.MAX_DOCUMENTS + 1)


@pytest.mark.skipif(not (runtime.PROJECT_ROOT / "Start-PADOS-P6-15-Corpus.ps1").is_file(),
                    reason="milestone-specific launcher, not part of the public export")
def test_launcher_uses_literal_argument_forwarding_and_isolated_target() -> None:
    launcher = (runtime.PROJECT_ROOT / "Start-PADOS-P6-15-Corpus.ps1").read_text(encoding="utf-8")
    assert "Invoke" + "-Expression" not in launcher
    assert "Get-Command python" in launcher
    assert "PADOS_CORPUS_DATABASE_NAME" in launcher
    assert "pados_" + "p6_15_" + "corpus_pilot" not in launcher


def test_search_scope_rejects_other_project_without_database_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "PROJECT_ID", "00000000-0000-0000-0000-000000000015")
    monkeypatch.setattr(runtime, "_doctor_or_raise", lambda: {"provenance": {}})
    with pytest.raises(runtime.CorpusRuntimeBlocked, match="PROJECT_SCOPE_MISMATCH"):
        runtime.search(query="research", project_id="00000000-0000-0000-0000-000000000000")


def test_unsupported_keyword_query_returns_no_fabricated_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyResult:
        def fetchall(self) -> list[object]:
            return []

    class FakeConnection:
        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, *_args: object) -> EmptyResult:
            return EmptyResult()

    monkeypatch.setattr(runtime, "_doctor_or_raise", lambda: {"provenance": {}})
    monkeypatch.setattr(runtime, "_connect", lambda _database: FakeConnection())
    monkeypatch.setattr(runtime, "PROJECT_ID", "00000000-0000-0000-0000-000000000015")
    result = runtime.search(query="__PADOS_UNSUPPORTED_QUERY__")
    assert result["result_count"] == 0
    assert result["results"] == []
