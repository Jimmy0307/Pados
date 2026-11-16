# SPDX-License-Identifier: AGPL-3.0-only
"""The document write-ahead journal on its own (no database): intent before staging, derived staged paths, the
classification of an audited intent, rollback order, and that the journal stays private and unlisted.

tests/integration/test_control_document_journal_postgres.py drives the same functions through the PostgreSQL commit.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from pados.control import project_documents as documents
from pados.control.product_errors import ProductControlError


@pytest.fixture
def root(tmp_path: Path) -> documents.ProjectRoot:
    workspace = tmp_path / "workspace"
    (workspace / "demo-12345678").mkdir(parents=True)
    return documents.resolve_project_root(str(workspace / "demo-12345678"), str(workspace))


def _stage(root: documents.ProjectRoot, data: bytes = b"one\n", **extra: object) -> documents.JournalEntry:
    options: dict[str, object] = {"replace": False, "expected_sha256": None, "idempotency_key": "k-1", **extra}
    return documents.stage_document(root, documents.validate_document_path("docs/guide.md"), data,
                                    **options)  # type: ignore[arg-type]


def _journal(root: documents.ProjectRoot) -> Path:
    return root.path.joinpath(*documents.JOURNAL_PARTS)


def test_the_intent_is_durable_before_the_staged_file_and_the_target_is_untouched(
        root: documents.ProjectRoot, monkeypatch: pytest.MonkeyPatch) -> None:
    original = documents._write_staged
    seen: list[list[str]] = []

    def observe(path: Path, data: bytes) -> None:
        seen.append(sorted(p.name for p in _journal(root).glob("*.json")))
        original(path, data)

    monkeypatch.setattr(documents, "_write_staged", observe)
    entry = _stage(root)
    assert seen == [[f"{entry.intent_id}.json"]]
    recorded = json.loads((_journal(root) / f"{entry.intent_id}.json").read_bytes())
    assert recorded == {"version": 1, "intent_id": entry.intent_id, "idempotency_key": "k-1", "path": "docs/guide.md",
                        "sha256": hashlib.sha256(b"one\n").hexdigest(), "previous_sha256": None, "size": 4,
                        "replace": False, "staged": f"docs/.guide.md.{entry.intent_id}.staged",
                        "created_at": entry.created_at}
    assert (root.path / entry.staged).read_bytes() == b"one\n"
    assert not (root.path / "docs" / "guide.md").exists()
    assert documents.open_entries(root) == ([entry], 0) and documents.has_open_entries(root)


def test_staging_keeps_every_write_precondition(root: documents.ProjectRoot) -> None:
    (root.path / "docs").mkdir()
    (root.path / "docs" / "guide.md").write_bytes(b"one\n")
    with pytest.raises(ProductControlError) as exists:
        _stage(root, b"two\n")
    assert exists.value.code == "DOCUMENT_EXISTS"
    with pytest.raises(ProductControlError) as stale:
        _stage(root, b"two\n", replace=True, expected_sha256="0" * 64)
    assert stale.value.code == "DOCUMENT_CHANGED"
    assert documents.open_entries(root) == ([], 0)  # a refused write records no intent
    entry = _stage(root, b"two\n", replace=True, expected_sha256=hashlib.sha256(b"one\n").hexdigest())
    assert entry.previous_sha256 == hashlib.sha256(b"one\n").hexdigest()


def test_apply_classifies_an_audited_intent(root: documents.ProjectRoot) -> None:
    target = root.path / "docs" / "guide.md"
    applied = _stage(root)
    assert documents.apply_entry(root.path, applied) == documents.APPLIED
    assert target.read_bytes() == b"one\n" and documents.open_entries(root) == ([], 0)
    assert not (root.path / applied.staged).exists()

    def replacing(data: bytes) -> documents.JournalEntry:
        return _stage(root, data, replace=True, expected_sha256=hashlib.sha256(target.read_bytes()).hexdigest())

    already = replacing(b"two\n")
    target.write_bytes(b"two\n")  # replaced, then the process died before the intent was closed
    assert documents.apply_entry(root.path, already) == documents.ALREADY_APPLIED
    missing = replacing(b"three\n")
    (root.path / missing.staged).unlink()
    assert documents.apply_entry(root.path, missing) == "DOCUMENT_STAGED_MISSING"
    corrupt = replacing(b"four\n")
    (root.path / corrupt.staged).write_bytes(b"f0ur\n")
    assert documents.apply_entry(root.path, corrupt) == "DOCUMENT_STAGED_CORRUPT"
    changed = replacing(b"five\n")
    target.write_bytes(b"edited elsewhere\n")
    assert documents.apply_entry(root.path, changed) == "DOCUMENT_TARGET_CHANGED"
    assert target.read_bytes() == b"edited elsewhere\n"  # never overwritten


def test_discard_removes_the_staged_file_then_the_intent(root: documents.ProjectRoot,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _stage(root)
    order: list[str] = []
    original_close = documents._close_entry

    def close(root_path: Path, intent_id: str) -> None:
        order.append("staged present" if (root.path / entry.staged).exists() else "staged gone")
        original_close(root_path, intent_id)

    monkeypatch.setattr(documents, "_close_entry", close)
    documents.discard_entry(root.path, entry)
    assert order == ["staged gone"] and documents.open_entries(root) == ([], 0)
    documents.discard_entry(root.path, entry)  # idempotent


def test_a_tampered_entry_is_never_acted_on(root: documents.ProjectRoot) -> None:
    entry = _stage(root)
    path = _journal(root) / f"{entry.intent_id}.json"
    raw = json.loads(path.read_bytes())
    for field, value in (("staged", "../../outside.staged"), ("path", "../x.md"), ("sha256", "nope"),
                         ("version", 2)):
        path.write_bytes(json.dumps({**raw, field: value}).encode())
        assert documents.open_entries(root) == ([], 1)
    path.write_bytes(b"{not json")
    assert documents.open_entries(root) == ([], 1)
    path.rename(_journal(root) / f"{'0' * 32}.json")  # a name that is not its intent id
    (_journal(root) / f"{'0' * 32}.json").write_bytes(json.dumps(raw).encode())
    assert documents.open_entries(root) == ([], 1)


def test_the_journal_is_private_unlisted_and_unreachable_by_name(root: documents.ProjectRoot) -> None:
    _stage(root)
    (root.path / "README.md").write_bytes(b"# Demo\n")
    assert [d["path"] for d in documents.list_documents(root)["documents"]] == ["README.md"]
    for name in (".pados/journal/x.md", "docs/.guide.md.staged"):
        with pytest.raises(ProductControlError):
            documents.validate_document_path(name)
    if sys.platform == "win32":
        from pados.identity import fs_security

        assert fs_security.problem(_journal(root)) is None


def test_a_read_only_root_has_no_journal(tmp_path: Path) -> None:
    elsewhere = tmp_path / "user-repo"
    elsewhere.joinpath(*documents.JOURNAL_PARTS).mkdir(parents=True)
    elsewhere.joinpath(*documents.JOURNAL_PARTS, f"{'a' * 32}.json").write_text("{}", encoding="utf-8")
    root = documents.resolve_project_root(str(elsewhere), str(tmp_path / "workspace"))
    assert documents.open_entries(root) == ([], 0) and not documents.has_open_entries(root)


def test_interrupted_intent_records_are_swept(root: documents.ProjectRoot) -> None:
    entry = _stage(root)
    leftover = _journal(root) / f".{entry.intent_id}.json.0a1b2c3d.tmp"
    leftover.write_bytes(b"{")
    assert documents.sweep_interrupted_intents(root) == 1 and not leftover.exists()
    assert documents.open_entries(root) == ([entry], 0)


def test_the_journal_folder_must_not_be_a_link(root: documents.ProjectRoot, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root.path / ".pados"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            pytest.skip("creating a symbolic link needs a privilege this host does not grant")
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))
    with pytest.raises(ProductControlError) as excinfo:
        _stage(root)
    assert excinfo.value.code == "DOCUMENT_JOURNAL_UNAVAILABLE"
    assert list(outside.iterdir()) == [] and not (root.path / "docs" / "guide.md").exists()
