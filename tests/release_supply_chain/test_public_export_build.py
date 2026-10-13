"""Public export builder and public-branch preparation, against temporary git repositories (no network, no push)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

from .support import ROOT

MARKER = "acme" + "widgetco"  # synthetic organisation marker, assembled at runtime
GITHUB_TOKEN_SHAPE = "ghp" + "_" + "A1b2C3d4" * 5
CRLF_BYTES = b"line one\r\nline two\r\n"
EXPORT_MANIFEST = {
    "schema_version": "pados.public-export-manifest.v1",
    "exclude": [{"pattern": "docs/private/**", "reason": "private"},
                {"pattern": "runtime/**", "reason": "evidence", "keep": ["runtime/**/.gitkeep"]}],
    "allowed_findings": [],
    "release_blocker_findings": [],
}
GIT_IDENTITY = ("-c", "user.name=Export Test", "-c", "user.email=export-test@example.com", "-c", "commit.gpgsign=false",
                "-c", "core.autocrlf=false", "-c", "init.defaultBranch=main")


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "release" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *GIT_IDENTITY, *args], cwd=repo, capture_output=True, check=True)
    return done.stdout.decode("utf-8").strip()


def _write(root: Path, files: dict[str, bytes]) -> None:
    for path, data in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def make_internal(tmp: Path, extra: dict[str, bytes] | None = None) -> tuple[Path, str]:
    repo = tmp / "internal"
    repo.mkdir()
    git(repo, "init", "-q")
    files = {
        "scripts/release/public_export_manifest.json": json.dumps(EXPORT_MANIFEST).encode(),
        "scripts/release/secret_scan_allowlist.json": b"[]\n",
        "README.md": b"# demo\n",
        "src/demo/core.py": b"VALUE = 1\n",
        "data/crlf.txt": CRLF_BYTES,
        "tools/run.sh": b"#!/bin/sh\necho ok\n",
        "docs/private/notes.md": f"internal notes for {MARKER}\n".encode(),
        "runtime/cache/state.json": b"{}\n",
        "runtime/logs/.gitkeep": b"",
        **(extra or {}),
    }
    _write(repo, files)
    git(repo, "add", "-A")
    git(repo, "update-index", "--chmod=+x", "tools/run.sh")
    git(repo, "commit", "-q", "-m", "internal")
    return repo, git(repo, "rev-parse", "HEAD")


@pytest.fixture
def markers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "private_markers.json"
    path.write_text(json.dumps({"company_identifier": MARKER}), encoding="utf-8")
    monkeypatch.setenv("PADOS_PRIVATE_MARKERS_FILE", str(path))
    return path


def _manifest(out: Path) -> dict[str, object]:
    return json.loads(out.with_name(out.name + ".manifest.json").read_text(encoding="utf-8"))


def test_build_refuses_without_private_marker_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                   capsys: pytest.CaptureFixture[str]) -> None:
    repo, commit = make_internal(tmp_path)
    monkeypatch.setenv("PADOS_PRIVATE_MARKERS_FILE", str(tmp_path / "missing.json"))
    out = tmp_path / "export"
    assert _load("build_public_export").build(commit, out, repo=repo) == 2
    assert not out.exists()
    assert "private marker file missing" in capsys.readouterr().err


def test_build_without_markers_needs_the_explicit_flag_and_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    repo, commit = make_internal(tmp_path)
    monkeypatch.setenv("PADOS_PRIVATE_MARKERS_FILE", str(tmp_path / "missing.json"))
    out = tmp_path / "export"
    assert _load("build_public_export").main([commit, str(out), "--repo", str(repo), "--no-private-markers"]) == 0
    assert "WARNING: --no-private-markers" in capsys.readouterr().err
    record = _manifest(out)
    assert record["private_marker_rules_loaded"] is False and record["no_private_markers_override"] is True


def test_build_is_byte_exact_excludes_private_paths_and_records_scans(tmp_path: Path, markers: Path) -> None:
    repo, commit = make_internal(tmp_path)
    out = tmp_path / "export"
    assert _load("build_public_export").build(commit, out, repo=repo) == 0
    record = _manifest(out)
    files = record["files"]
    assert isinstance(files, dict)
    assert sorted(files) == ["README.md", "data/crlf.txt", "runtime/logs/.gitkeep", "scripts/release/public_export_manifest.json",
                             "scripts/release/secret_scan_allowlist.json", "src/demo/core.py", "tools/run.sh"]
    assert not (out / "docs" / "private").exists() and not (out / "runtime" / "cache").exists()
    assert (out / "data" / "crlf.txt").read_bytes() == CRLF_BYTES
    for path, digest in files.items():
        assert hashlib.sha256((out / path).read_bytes()).hexdigest() == digest
    assert record["modes"]["tools/run.sh"] == "100755" and record["modes"]["README.md"] == "100644"  # type: ignore[index]
    assert record["commit"] == commit and record["private_marker_rules_loaded"] is True
    assert record["private_marker_rule_names"] == ["company_identifier"]
    scans = record["scans"]
    assert isinstance(scans, dict) and scans["status"] == "PASS" and scans["values_recorded"] is False


def test_build_fails_on_a_private_marker_in_the_export_without_recording_it(tmp_path: Path, markers: Path) -> None:
    repo, commit = make_internal(tmp_path, {"docs/guide.md": f"Validated at {MARKER} plant.\n".encode()})
    out = tmp_path / "export"
    assert _load("build_public_export").build(commit, out, repo=repo) == 1
    text = out.with_name(out.name + ".manifest.json").read_text(encoding="utf-8")
    scans = json.loads(text)["scans"]
    assert scans["status"] == "FAIL"
    assert [(f["path"], f["rule"]) for f in scans["export_check"]["unclassified"]] == [("docs/guide.md",
                                                                                         "company_identifier")]
    assert MARKER not in text.lower()


def test_build_fails_on_an_unclassified_secret_in_the_export(tmp_path: Path, markers: Path) -> None:
    repo, commit = make_internal(tmp_path, {"src/demo/settings.py": f"KEY = '{GITHUB_TOKEN_SHAPE}'\n".encode()})
    out = tmp_path / "export"
    assert _load("build_public_export").build(commit, out, repo=repo) == 1
    text = out.with_name(out.name + ".manifest.json").read_text(encoding="utf-8")
    scans = json.loads(text)["scans"]
    assert [(f["path"], f["rule"]) for f in scans["secret_scan"]["unclassified"]] == [("src/demo/settings.py",
                                                                                        "GITHUB_TOKEN")]
    assert GITHUB_TOKEN_SHAPE not in text


def test_build_uses_the_manifest_at_the_commit_and_refuses_an_existing_output(tmp_path: Path, markers: Path) -> None:
    repo, commit = make_internal(tmp_path)
    (repo / "scripts/release/public_export_manifest.json").write_text("{}", encoding="utf-8")  # working tree only
    out = tmp_path / "export"
    builder = _load("build_public_export")
    assert builder.build(commit, out, repo=repo) == 0
    assert builder.build(commit, out, repo=repo) == 2


def make_public_clone(tmp: Path) -> Path:
    remote = tmp / "public.git"
    git(tmp, "init", "-q", "--bare", str(remote))
    seed = tmp / "seed"
    seed.mkdir()
    git(seed, "init", "-q")
    _write(seed, {"OLD_FILE.md": b"previous public content\n", ".gitattributes": b"* text=auto eol=crlf\n"})
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "public root")
    git(seed, "push", "-q", str(remote), "HEAD:refs/heads/main")
    clone = tmp / "clone"
    git(tmp, "clone", "-q", str(remote), str(clone))
    for key, value in (("user.name", "Export Test"), ("user.email", "export-test@example.com"),
                       ("commit.gpgsign", "false"), ("core.autocrlf", "true")):
        git(clone, "config", key, value)
    return clone


def _refs(clone: Path) -> str:
    return git(clone, "for-each-ref", "--format=%(refname) %(objectname)")


def test_prepare_public_branch_creates_a_local_branch_with_the_exact_export_tree(tmp_path: Path, markers: Path) -> None:
    repo, commit = make_internal(tmp_path)
    clone = make_public_clone(tmp_path)
    base = git(clone, "rev-parse", "origin/main")
    head_before, refs_before = git(clone, "rev-parse", "HEAD"), _refs(clone)
    result = _load("prepare_public_branch").prepare(commit, clone, branch="export/v1", message="Public export v1",
                                                    repo=repo)
    assert result["pushed"] is False and result["base"] == base and result["internal_commit"] == commit
    new = str(result["commit"])
    assert git(clone, "rev-parse", "refs/heads/export/v1") == new
    assert git(clone, "rev-parse", f"{new}^") == base
    assert git(clone, "rev-parse", f"{new}^{{tree}}") == result["tree"]
    assert git(clone, "log", "-1", "--format=%s", new) == "Public export v1"
    listed = git(clone, "ls-tree", "-r", "--name-only", new).splitlines()
    assert "OLD_FILE.md" not in listed and "docs/private/notes.md" not in listed and "tools/run.sh" in listed
    blob = subprocess.run(["git", "cat-file", "blob", f"{new}:data/crlf.txt"], cwd=clone, capture_output=True,
                          check=True).stdout
    assert blob == CRLF_BYTES
    assert "100755 blob" in git(clone, "ls-tree", new, "tools/run.sh")
    # Nothing else changed: HEAD, remote-tracking refs, working tree.
    assert git(clone, "rev-parse", "HEAD") == head_before
    refs_after = [line for line in _refs(clone).splitlines() if not line.startswith("refs/heads/export/v1 ")]
    assert refs_after == refs_before.splitlines()
    assert (clone / "OLD_FILE.md").is_file() and git(clone, "status", "--porcelain") == ""


def test_prepare_public_branch_refuses_existing_branch_local_base_and_failed_scans(tmp_path: Path,
                                                                                   markers: Path) -> None:
    repo, commit = make_internal(tmp_path, {"docs/guide.md": f"{MARKER}\n".encode()})
    clone = make_public_clone(tmp_path)
    prepare = _load("prepare_public_branch").prepare
    git(clone, "branch", "taken")
    with pytest.raises(SystemExit, match="already exists"):
        prepare(commit, clone, branch="taken", message="m", repo=repo)
    with pytest.raises(SystemExit, match="not a remote-tracking ref"):
        prepare(commit, clone, branch="fresh", message="m", repo=repo, base="main")
    with pytest.raises(SystemExit, match="public export build failed"):
        prepare(commit, clone, branch="fresh", message="m", repo=repo)
    assert subprocess.run(["git", "show-ref", "--verify", "--quiet", "refs/heads/fresh"], cwd=clone,
                          check=False).returncode != 0
    with pytest.raises(SystemExit, match="internal repository"):
        prepare(commit, repo, branch="fresh", message="m", repo=repo)


def test_public_branch_commit_uses_the_fixed_public_identity_in_utc(tmp_path: Path, markers: Path) -> None:
    """Red-team F-8: the clone's own identity (here export-test@example.com) never enters public history."""
    repo, commit = make_internal(tmp_path)
    clone = make_public_clone(tmp_path)
    module = _load("prepare_public_branch")
    result = module.prepare(commit, clone, branch="export/v2", message="Public export v2", repo=repo)
    fields = git(clone, "log", "-1", "--format=%an|%ae|%cn|%ce|%ad|%cd", "--date=format:%z", str(result["commit"]))
    assert fields == "PADOS Release|release@local.invalid|PADOS Release|release@local.invalid|+0000|+0000"
    assert result["identity"] == module.PUBLIC_IDENTITY
    other = module.prepare(commit, clone, branch="export/v3", message="v3", repo=repo,
                           identity="The PADOS Authors <12345+pados" + "@" + "users.noreply.github.com>")
    assert git(clone, "log", "-1", "--format=%an <%ae>", str(other["commit"])) == \
        "The PADOS Authors <12345+pados" + "@" + "users.noreply.github.com>"


@pytest.mark.parametrize("identity", ["Export Test <export-test@example.com>", "Someone <me" + "@" + "corp.internal>",
                                      "no-brackets@local.invalid", "<release@local.invalid>",
                                      f"{MARKER} <release@local.invalid>", f"Release <{MARKER}@pados.invalid>"])
def test_public_branch_refuses_a_routable_or_private_identity(tmp_path: Path, markers: Path, identity: str) -> None:
    repo, commit = make_internal(tmp_path)
    clone = make_public_clone(tmp_path)
    with pytest.raises(SystemExit, match="REFUSED"):
        _load("prepare_public_branch").prepare(commit, clone, branch="fresh", message="m", repo=repo,
                                               identity=identity)
    assert subprocess.run(["git", "show-ref", "--verify", "--quiet", "refs/heads/fresh"], cwd=clone,
                          check=False).returncode != 0


@pytest.mark.parametrize("message, rule", [
    (f"Export for {MARKER} release", "company_identifier"),
    ("Thanks to alice" + "@" + "corp-mail.net", "email"),
    ("Built on " + "C:" + "\\Users\\alice\\src", "local_user_path"),
    ("Token " + GITHUB_TOKEN_SHAPE, "GITHUB_TOKEN"),
    ("", "empty"),
])
def test_public_branch_refuses_a_commit_message_that_fails_the_export_scan(tmp_path: Path, markers: Path,
                                                                           message: str, rule: str) -> None:
    """Red-team F-8: the commit message is scanned with the export-check rules (private markers included) and the
    secret-scan rules; the refusal names rules only, never the matched value."""
    repo, commit = make_internal(tmp_path)
    clone = make_public_clone(tmp_path)
    with pytest.raises(SystemExit, match="REFUSED") as refused:
        _load("prepare_public_branch").prepare(commit, clone, branch="fresh", message=message, repo=repo)
    text = str(refused.value)
    if message:
        assert rule in text and message not in text
    assert subprocess.run(["git", "show-ref", "--verify", "--quiet", "refs/heads/fresh"], cwd=clone,
                          check=False).returncode != 0


def test_release_scripts_never_push() -> None:
    for name in ("build_public_export.py", "prepare_public_branch.py"):
        source = (ROOT / "scripts" / "release" / name).read_text(encoding="utf-8")
        assert '"push"' not in source and "'push'" not in source, name
