# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded Markdown documents in a project folder: names, size, encoding, traversal, links, overwrite protection."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from pados.control import project_documents as documents
from pados.control.product_errors import ProductControlError


def _code(excinfo: pytest.ExceptionInfo[ProductControlError]) -> tuple[str, int]:
    return excinfo.value.code, excinfo.value.status


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "demo-12345678").mkdir(parents=True)
    return root


def _root(workspace: Path) -> documents.ProjectRoot:
    return documents.resolve_project_root(str(workspace / "demo-12345678"), str(workspace))


@pytest.mark.parametrize("name", ["notes.md", "docs/guide.md", "a/b/c.md", "Release_Notes-1.2.md"])
def test_safe_names_are_accepted(name: str) -> None:
    assert documents.validate_document_path(name).as_posix() == name


@pytest.mark.parametrize("name", [
    "", "../escape.md", "docs/../../x.md", "/abs.md", "C:/x.md", "docs\\x.md", "notes.txt", ".md", ".hidden.md",
    "a/b/c/d.md", "con.md", "docs/nul.md", "x\x00.md", "space name.md", "docs/.git/x.md", "trailing./x.md",
    "notes.md.exe", "a" * 70 + ".md",
])
def test_unsafe_names_are_refused(name: str) -> None:
    with pytest.raises(ProductControlError) as excinfo:
        documents.validate_document_path(name)
    assert _code(excinfo) == ("DOCUMENT_PATH_INVALID", 422)


def test_content_limits_and_checks() -> None:
    assert documents.encode_content("# 標題\n") == "# 標題\n".encode()
    assert len(documents.encode_content("x" * documents.DOCUMENT_MAX_BYTES)) == documents.DOCUMENT_MAX_BYTES
    cases = {
        "x" * (documents.DOCUMENT_MAX_BYTES + 1): ("DOCUMENT_TOO_LARGE", 413),
        "中" * (documents.DOCUMENT_MAX_BYTES // 3 + 1): ("DOCUMENT_TOO_LARGE", 413),  # bytes, not characters
        "a\x00b": ("DOCUMENT_CONTENT_INVALID", 422),
        "\ud800": ("DOCUMENT_ENCODING_INVALID", 422),
        "see https://user:" + "hunter2x@example.com/x": ("DOCUMENT_CONTENT_REJECTED", 422),
    }
    for content, expected in cases.items():
        with pytest.raises(ProductControlError) as excinfo:
            documents.encode_content(content)
        assert _code(excinfo) == expected


def test_create_then_refuse_overwrite_then_replace_with_the_current_digest(workspace: Path) -> None:
    root = _root(workspace)
    first = documents.write_document(root, documents.validate_document_path("docs/guide.md"), b"one\n",
                                     replace=False, expected_sha256=None)
    assert first == {"path": "docs/guide.md", "size": 4, "sha256": hashlib.sha256(b"one\n").hexdigest(),
                     "replaced": False, "previous_sha256": None}
    target = workspace / "demo-12345678" / "docs" / "guide.md"
    assert target.read_bytes() == b"one\n"
    relative = documents.validate_document_path("docs/guide.md")
    with pytest.raises(ProductControlError) as exists:
        documents.write_document(root, relative, b"two\n", replace=False, expected_sha256=None)
    assert _code(exists) == ("DOCUMENT_EXISTS", 409)
    with pytest.raises(ProductControlError) as stale:
        documents.write_document(root, relative, b"two\n", replace=True, expected_sha256="0" * 64)
    assert _code(stale) == ("DOCUMENT_CHANGED", 409)
    with pytest.raises(ProductControlError) as missing_digest:
        documents.write_document(root, relative, b"two\n", replace=True, expected_sha256=None)
    assert _code(missing_digest) == ("DOCUMENT_CHANGED", 409)
    assert target.read_bytes() == b"one\n"
    second = documents.write_document(root, relative, b"two\n", replace=True, expected_sha256=first["sha256"])
    assert second["replaced"] is True and second["previous_sha256"] == first["sha256"]
    assert target.read_bytes() == b"two\n"
    assert [p.name for p in target.parent.iterdir()] == ["guide.md"]  # no temporary file left behind
    with pytest.raises(ProductControlError) as absent:
        documents.write_document(root, documents.validate_document_path("other.md"), b"x", replace=True,
                                 expected_sha256=first["sha256"])
    assert _code(absent) == ("DOCUMENT_NOT_FOUND", 404)


def test_a_root_outside_the_workspace_is_read_only(tmp_path: Path, workspace: Path) -> None:
    elsewhere = tmp_path / "user-repo"
    elsewhere.mkdir()
    root = documents.resolve_project_root(str(elsewhere), str(workspace))
    assert root.writable is False
    with pytest.raises(ProductControlError) as excinfo:
        documents.write_document(root, documents.validate_document_path("x.md"), b"x", replace=False,
                                 expected_sha256=None)
    assert _code(excinfo) == ("PROJECT_ROOT_OUTSIDE_WORKSPACE", 403)
    assert not (elsewhere / "x.md").exists()
    # The workspace root itself is not a project folder either.
    assert documents.resolve_project_root(str(workspace), str(workspace)).writable is False
    assert documents.resolve_project_root(str(elsewhere), "").writable is False


def test_a_missing_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProductControlError) as excinfo:
        documents.resolve_project_root(str(tmp_path / "gone"), str(tmp_path))
    assert _code(excinfo) == ("PROJECT_ROOT_UNAVAILABLE", 409)


@pytest.mark.skipif(os.name == "nt" and not hasattr(os, "symlink"), reason="no symlink support")
def test_a_link_inside_the_project_is_never_followed(tmp_path: Path, workspace: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "demo-12345678" / "docs"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            pytest.skip("creating a symbolic link needs a privilege this host does not grant")
        import _winapi  # a directory junction needs no privilege on Windows and must be refused the same way

        _winapi.CreateJunction(str(outside), str(link))
    root = _root(workspace)
    with pytest.raises(ProductControlError) as excinfo:
        documents.write_document(root, documents.validate_document_path("docs/x.md"), b"x", replace=False,
                                 expected_sha256=None)
    assert _code(excinfo) == ("DOCUMENT_PATH_OUTSIDE_PROJECT", 403)
    assert not (outside / "x.md").exists()
    listed = documents.list_documents(root)
    assert listed["documents"] == []


def test_list_and_read(workspace: Path) -> None:
    project = workspace / "demo-12345678"
    (project / "README.md").write_bytes(b"# Demo\n")
    (project / "docs").mkdir()
    (project / "docs" / "guide.md").write_bytes(b"[a](../README.md)\n")
    (project / "notes.txt").write_text("ignored", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git" / "x.md").write_text("ignored", encoding="utf-8")
    listed = documents.list_documents(_root(workspace))
    assert [d["path"] for d in listed["documents"]] == ["README.md", "docs/guide.md"]
    assert listed["writable"] is True and listed["truncated"] is False
    assert listed["documents"][0]["sha256"] == hashlib.sha256(b"# Demo\n").hexdigest()
    read = documents.read_document(_root(workspace), documents.validate_document_path("docs/guide.md"))
    assert read["content"] == "[a](../README.md)\n" and read["size"] == 18
    with pytest.raises(ProductControlError) as missing:
        documents.read_document(_root(workspace), documents.validate_document_path("nope.md"))
    assert _code(missing) == ("DOCUMENT_NOT_FOUND", 404)


def test_expected_sha_must_be_hex() -> None:
    assert documents.expected_sha(None) is None and documents.expected_sha("") is None
    assert documents.expected_sha("A" * 64) == "a" * 64
    with pytest.raises(ProductControlError) as excinfo:
        documents.expected_sha("../etc")
    assert _code(excinfo) == ("DOCUMENT_SHA256_INVALID", 422)
