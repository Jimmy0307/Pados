# SPDX-License-Identifier: AGPL-3.0-only
"""OP-010: references in docs must resolve; the lint reports real dangling references and nothing else."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from pados.release.doc_lint import ABSENT_PATHS_MARKER, default_docs, lint, lint_file

ROOT = Path(__file__).resolve().parents[2]


def _tree(tmp_path: Path, doc: str) -> Path:
    (tmp_path / "src" / "pados").mkdir(parents=True)
    (tmp_path / "src" / "pados" / "real.py").write_text("class Thing: ...\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "other.md").write_text("# Other\n", encoding="utf-8")
    (tmp_path / "docs" / "doc.md").write_text(doc, encoding="utf-8")
    return tmp_path / "docs" / "doc.md"


def _rules(tmp_path: Path, doc: str) -> list[tuple[int, str, str]]:
    path = _tree(tmp_path, doc)
    return [(f.line, f.rule, f.target) for f in lint_file(tmp_path, path)]


def test_resolving_references_pass(tmp_path: Path) -> None:
    doc = ("See [other](other.md#top), `src/pados/real.py`, `src/pados/real.py:12-40`, `src/pados/real.Thing`,\n"
           "`tests/x.py::test_a` is not checked here, [web](https://example.com), `<sha>` and `src/pados/*.py`.\n")
    assert [r for r in _rules(tmp_path, doc) if "tests/x.py" not in r[2]] == []


def test_dangling_links_and_paths_are_named_with_their_line(tmp_path: Path) -> None:
    doc = "# Doc\n\n[gone](missing.md) and `src/pados/absent.py`.\n"
    assert _rules(tmp_path, doc) == [(3, "LINK_TARGET_MISSING", "missing.md"),
                                     (3, "CODE_PATH_MISSING", "src/pados/absent.py")]


def test_build_outputs_absent_from_a_clean_checkout_are_not_checked(tmp_path: Path) -> None:
    # A clean CI checkout has no web/dist; the lint must give the same answer there as after a local build.
    assert _rules(tmp_path, "The workflow uploads `web/dist` and installs `web/node_modules/vite`.\n") == []


def test_fenced_examples_are_not_checked(tmp_path: Path) -> None:
    assert _rules(tmp_path, "```text\n`src/pados/future.py` [x](nope.md)\n```\n") == []


def test_the_absent_paths_marker_turns_off_only_the_code_path_check(tmp_path: Path) -> None:
    doc = f"# Excluded\n\n{ABSENT_PATHS_MARKER}\n\n`src/pados/excluded.py` [gone](missing.md)\n"
    assert _rules(tmp_path, doc) == [(5, "LINK_TARGET_MISSING", "missing.md")]


def test_the_repository_docs_have_no_dangling_references() -> None:
    findings = lint(ROOT, default_docs(ROOT))
    assert [str(f) for f in findings] == []


def test_the_cli_fails_on_a_finding(tmp_path: Path) -> None:
    _tree(tmp_path, "[gone](missing.md)\n")
    script = ROOT / "scripts" / "release" / "doc_lint.py"
    result = subprocess.run([sys.executable, str(script), str(tmp_path / "out.json"), "--root", str(tmp_path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1 and "DOC_LINT_FINDINGS" in result.stdout and "missing.md" in result.stdout
