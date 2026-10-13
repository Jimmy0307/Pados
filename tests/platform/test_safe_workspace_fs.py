"""SafeWorkspaceFS confinement suite.

Common cases run on every platform against the native backend. Windows-only
cases (junctions, short names, device names, streams, UNC roots, hard links)
and POSIX-only cases (FIFOs) run only on their own platform; nothing is
simulated on the other one.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pados.execution.handoff import task_envelope
from pados.execution.receipts import artifact_handoff, verify_handoff_files
from pados.execution.safe_fs import SafeFSError, workspace_fs

WINDOWS = sys.platform == "win32"
windows_only = pytest.mark.skipif(not WINDOWS, reason="WINDOWS_NATIVE_SUITE")
posix_only = pytest.mark.skipif(WINDOWS, reason="POSIX_SUITE")


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    root = (tmp_path / "workspace").resolve()
    outside = (tmp_path / "outside").resolve()
    (root / "nested" / "deeper").mkdir(parents=True)
    outside.mkdir()
    (root / "a.txt").write_bytes(b"inside")
    (root / "nested" / "deeper" / "b.txt").write_bytes(b"nested")
    (outside / "secret.txt").write_bytes(b"outside")
    return root, outside


def _symlink_or_skip(link: Path, target: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError as exc:  # Windows without SeCreateSymbolicLinkPrivilege / Developer Mode
        pytest.skip(f"SYMLINK_PRIVILEGE_UNAVAILABLE: {exc}")


# 1-2
def test_normal_and_nested_files_are_accepted(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    fs = workspace_fs(root)
    assert fs.read_file("a.txt") == b"inside"
    assert fs.read_file("nested/deeper/b.txt") == b"nested"
    assert fs.list_regular_files() == {"a.txt", "nested/deeper/b.txt"}
    fs.write_file("nested/new/c.txt", b"written")
    assert fs.read_file("nested/new/c.txt") == b"written"


# 3-4
@pytest.mark.parametrize("relative", ["../outside/secret.txt", "nested/../../outside/secret.txt", "/etc/passwd",
                                      "C:/Windows/win.ini", "nested\\..\\..\\x", "", "./a.txt"])
def test_outside_and_dotdot_escapes_are_rejected(layout: tuple[Path, Path], relative: str) -> None:
    root, _ = layout
    fs = workspace_fs(root)
    with pytest.raises(SafeFSError):
        fs.read_file(relative)
    with pytest.raises(SafeFSError):
        fs.write_file(relative, b"x")


# 5
def test_symlink_escape_is_rejected(layout: tuple[Path, Path]) -> None:
    root, outside = layout
    _symlink_or_skip(root / "file-link.txt", outside / "secret.txt", directory=False)
    _symlink_or_skip(root / "dir-link", outside, directory=True)
    fs = workspace_fs(root)
    for relative in ("file-link.txt", "dir-link/secret.txt"):
        with pytest.raises(SafeFSError):
            fs.read_file(relative)
    with pytest.raises(SafeFSError):
        fs.write_file("dir-link/new.txt", b"x")
    with pytest.raises(SafeFSError):
        fs.list_regular_files()
    assert not (outside / "new.txt").exists()


# 6 (Windows junction / reparse point)
@windows_only
def test_junction_escape_is_rejected(layout: tuple[Path, Path]) -> None:
    import _winapi  # type: ignore[import-not-found,unused-ignore]

    root, outside = layout
    _winapi.CreateJunction(str(outside), str(root / "junction"))
    fs = workspace_fs(root)
    with pytest.raises(SafeFSError, match="LINK_REJECTED"):
        fs.read_file("junction/secret.txt")
    with pytest.raises(SafeFSError, match="LINK_REJECTED"):
        fs.write_file("junction/planted.txt", b"x")
    with pytest.raises(SafeFSError, match="LINK_REJECTED"):
        fs.list_regular_files()
    assert not (outside / "planted.txt").exists()


# 7-8
def test_missing_file_and_directory_as_file(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    fs = workspace_fs(root)
    assert fs.read_file("missing.txt") is None
    assert fs.read_file("missing-dir/missing.txt") is None
    assert fs.is_regular_file("missing.txt") is False
    with pytest.raises(SafeFSError):
        fs.read_file("nested")
    with pytest.raises(SafeFSError):
        fs.write_file("nested", b"x")
    with pytest.raises(SafeFSError):
        fs.read_file("a.txt/child.txt")


def test_oversized_file_fails_closed_unless_truncation_requested(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    fs = workspace_fs(root)
    with pytest.raises(SafeFSError, match="BUDGET"):
        fs.read_file("a.txt", max_bytes=3)
    assert fs.read_file("a.txt", max_bytes=3, allow_truncate=True) == b"insi"


# 9
def test_tampered_artifact_is_rejected(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    content = (root / "a.txt").read_bytes()
    artifact = {"path": "a.txt", "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
    assert verify_handoff_files(root, [artifact], ["a.txt"]) is True
    (root / "a.txt").write_bytes(b"tampered")
    assert verify_handoff_files(root, [artifact], ["a.txt"]) is False


# 10-11
def _contract(**overrides: object) -> dict[str, object]:
    return {"work_contract_id": "c", "state": "AUTHORIZED",
            "scope": {"isolated": True, "allowed_paths": ["a.py"], "workspace_root": "/approved"},
            "changes": [{"path": "a.py"}], "allowed_actions": ["WRITE_APPROVED_WORKSPACE"],
            "context_fingerprint": "hash", "semantic_base_scope_version": 0,
            "required_capabilities": ["FILE_WRITE"], "human_gate_required": False,
            "retry_policy": {"max_attempts": 1}, **overrides}


def test_duplicate_execution_paths_are_rejected() -> None:
    digest = "a" * 64
    with pytest.raises(ValueError, match="INVALID_ARTIFACT_PATH"):
        artifact_handoff([{"path": "a.py", "sha256": digest, "size_bytes": 1}] * 2)
    with pytest.raises(PermissionError):
        task_envelope(_contract(changes=[{"path": "a.py"}, {"path": "a.py"}]))


@pytest.mark.parametrize("protected", [".env", ".git/config", ".ssh/id_rsa"])
def test_protected_execution_paths_are_rejected(protected: str) -> None:
    contract = _contract(scope={"isolated": True, "allowed_paths": [protected], "workspace_root": "/approved"},
                         changes=[{"path": protected}])
    with pytest.raises(PermissionError):
        task_envelope(contract)


# 12
def test_root_mismatch_is_rejected(layout: tuple[Path, Path]) -> None:
    root, outside = layout
    content = (outside / "secret.txt").read_bytes()
    artifact = {"path": "secret.txt", "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)}
    assert verify_handoff_files(root, [artifact], ["secret.txt"]) is False
    assert verify_handoff_files(root / "absent-root", [artifact], ["secret.txt"]) is False


# 13 (alternate spellings)
@windows_only
def test_windows_alternate_spellings_do_not_bypass_confinement(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    fs = workspace_fs(root)
    for relative in ("A.TXT", "NESTED/deeper/b.txt", "a.txt.", "a.txt ", "a.txt::$DATA", "a.txt:stream",
                     "NESTED~1/deeper/b.txt", "nested/DEEPER~1/b.txt"):
        with pytest.raises(SafeFSError):
            fs.read_file(relative)
    for root_spelling in ("\\\\?\\" + str(root), "\\\\.\\" + str(root), "\\\\localhost\\c$\\x", "workspace", "C:workspace"):
        with pytest.raises(SafeFSError):
            workspace_fs(Path(root_spelling))


@windows_only
def test_windows_hard_link_alias_is_rejected(layout: tuple[Path, Path]) -> None:
    root, outside = layout
    os.link(outside / "secret.txt", root / "hard.txt")
    with pytest.raises(SafeFSError, match="HARD_LINK"):
        workspace_fs(root).read_file("hard.txt")


@posix_only
def test_posix_case_variant_is_a_distinct_missing_name(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    assert workspace_fs(root).read_file("A.TXT") is None


# 14 (special files)
@windows_only
@pytest.mark.parametrize("relative", ["NUL", "con.txt", "nested/AUX", "COM1.log", "LPT9", "CONIN$"])
def test_windows_device_names_fail_closed(layout: tuple[Path, Path], relative: str) -> None:
    root, _ = layout
    fs = workspace_fs(root)
    with pytest.raises(SafeFSError, match="DEVICE_NAME"):
        fs.read_file(relative)
    with pytest.raises(SafeFSError, match="DEVICE_NAME"):
        fs.write_file(relative, b"x")


@posix_only
def test_posix_fifo_fails_closed(layout: tuple[Path, Path]) -> None:
    root, _ = layout
    os.mkfifo(root / "pipe")
    fs = workspace_fs(root)
    with pytest.raises(SafeFSError):
        fs.read_file("pipe")
    with pytest.raises(SafeFSError):
        fs.list_regular_files()


def test_backend_matches_platform() -> None:
    backend = type(workspace_fs(Path.cwd().resolve())).__name__
    assert backend == ("WindowsWorkspaceFS" if WINDOWS else "PosixWorkspaceFS")
    assert subprocess.run([sys.executable, "-c", "print(1)"], capture_output=True, check=False).returncode == 0


def test_windows_path_helpers_are_platform_independent() -> None:
    from pados.execution.safe_fs import (
        windows_component_error,
        windows_final_path,
        windows_root_error,
    )

    assert windows_component_error("normal.txt") is None
    for bad in ("CON", "nul.txt", "a:b", "x.", "x ", "PROGRA~1", "a|b"):
        assert windows_component_error(bad) is not None
    assert windows_root_error("C:\\work") is None
    for bad in ("\\\\server\\share", "\\\\?\\C:\\x", "\\\\.\\PhysicalDrive0", "work", "C:work"):
        assert windows_root_error(bad) is not None
    assert windows_final_path("\\\\?\\C:\\work\\a.txt") == "C:\\work\\a.txt"
    assert windows_final_path("\\\\?\\UNC\\server\\share\\a.txt") is None
    assert windows_final_path("\\\\.\\pipe\\x") is None
