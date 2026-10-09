"""SPDX header tool (scripts/release/apply_spdx_headers.py) and the repository-wide header check."""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from .support import ROOT

TOOL = ROOT / "scripts/release/apply_spdx_headers.py"
AGPL_PY = "# SPDX-License-Identifier: AGPL-3.0-only"


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("apply_spdx_headers", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


spdx = _tool()


def _write(root: Path, relative: str, content: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_every_owned_source_file_carries_its_spdx_header() -> None:
    """Repository gate: fails until the integrator applies the headers (apply_spdx_headers.py)."""
    done = subprocess.run([sys.executable, str(TOOL), "--root", str(ROOT), "--check"],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout[-4000:]


@pytest.mark.parametrize(("relative", "expected"), [
    ("src/pados/x.py", ("AGPL-3.0-only", "#")),
    ("scripts/release/x.py", ("AGPL-3.0-only", "#")),
    ("web/src/app.ts", ("AGPL-3.0-only", "//")),
    ("web/src/app.tsx", ("AGPL-3.0-only", "//")),
    ("sdk/python/client.py", ("Apache-2.0", "#")),
    ("sdk/schemas/types.ts", ("Apache-2.0", "//")),
    ("src/pados/vendor/lib.py", None),
    ("THIRD_PARTY_LICENSES/x.py", None),
    ("web/src/node_modules/react/index.ts", None),
    ("web/dist/assets/index.ts", None),
    ("tests/test_x.py", None),
    ("src/pados/data.json", None),
    ("web/src/styles.css", None),
])
def test_scope_and_license_per_area(relative: str, expected: tuple[str, str] | None) -> None:
    target = spdx.classify(relative)
    assert (None if target is None else (target.license_id, target.comment)) == expected


def test_header_goes_before_the_docstring_and_keeps_it_a_docstring(tmp_path: Path) -> None:
    path = _write(tmp_path, "src/pados/m.py", b'"""Module doc."""\nfrom __future__ import annotations\n\nX = 1\n')
    assert spdx.apply(tmp_path) == ["src/pados/m.py"]
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == AGPL_PY
    assert ast.get_docstring(ast.parse(text)) == "Module doc."
    compile(text, "m.py", "exec")


def test_shebang_and_encoding_lines_stay_on_top(tmp_path: Path) -> None:
    path = _write(tmp_path, "scripts/run.py", b"#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\nprint(1)\n")
    spdx.apply(tmp_path)
    assert path.read_text(encoding="utf-8").splitlines()[:3] == [
        "#!/usr/bin/env python3", "# -*- coding: utf-8 -*-", AGPL_PY]


def test_bom_crlf_and_empty_files_are_preserved(tmp_path: Path) -> None:
    crlf = _write(tmp_path, "src/pados/w.py", b"\xef\xbb\xbfX = 1\r\nY = 2\r\n")
    empty = _write(tmp_path, "src/pados/__init__.py", b"")
    tsx = _write(tmp_path, "web/src/a.tsx", b"export const A = 1;\n")
    spdx.apply(tmp_path)
    assert crlf.read_bytes() == b"\xef\xbb\xbf" + AGPL_PY.encode() + b"\r\nX = 1\r\nY = 2\r\n"
    assert empty.read_bytes() == AGPL_PY.encode() + b"\n"
    assert tsx.read_text(encoding="utf-8").startswith("// SPDX-License-Identifier: AGPL-3.0-only\nexport")


def test_apply_is_idempotent_and_check_turns_green(tmp_path: Path) -> None:
    _write(tmp_path, "src/pados/a.py", b"A = 1\n")
    _write(tmp_path, "sdk/python/c.py", b"C = 1\n")
    _write(tmp_path, "src/pados/vendor/third.py", b"# Copyright someone else\n")
    assert [f.path for f in spdx.inspect(tmp_path)] == ["sdk/python/c.py", "src/pados/a.py"]
    assert spdx.main(["--root", str(tmp_path), "--check"]) == 1
    assert sorted(spdx.apply(tmp_path)) == ["sdk/python/c.py", "src/pados/a.py"]
    snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*.py")}
    assert spdx.apply(tmp_path) == []
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.py")} == snapshot
    assert spdx.main(["--root", str(tmp_path), "--check"]) == 0
    assert (tmp_path / "sdk/python/c.py").read_text(encoding="utf-8").startswith(
        "# SPDX-License-Identifier: Apache-2.0\n")
    assert (tmp_path / "src/pados/vendor/third.py").read_bytes() == b"# Copyright someone else\n"


def test_a_different_identifier_is_reported_and_never_rewritten(tmp_path: Path) -> None:
    original = b"# SPDX-License-Identifier: MIT\nX = 1\n"
    path = _write(tmp_path, "src/pados/other.py", original)
    findings = spdx.inspect(tmp_path)
    assert [(f.path, f.status) for f in findings] == [("src/pados/other.py", "MISMATCH")]
    assert spdx.main(["--root", str(tmp_path)]) == 1
    assert path.read_bytes() == original
