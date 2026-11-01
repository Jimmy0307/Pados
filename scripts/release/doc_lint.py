# SPDX-License-Identifier: AGPL-3.0-only
"""Documentation lint (OP-010). Usage: doc_lint.py OUT.json [--root DIR]

Exit 0 (DOC_LINT_OK) when every relative link and backticked repository path in docs/ and the root runbooks
resolves; exit 1 (DOC_LINT_FINDINGS) otherwise, listing path:line, rule and target. Read-only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pados.release.doc_lint import default_docs, lint  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out", type=Path)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    docs = default_docs(root)
    findings = lint(root, docs)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"schema_version": "pados.doc-lint.v1", "documents": len(docs),
                                    "findings": [vars(finding) for finding in findings]}, indent=2) + "\n",
                        encoding="utf-8")
    for finding in findings:
        print(finding)
    print(f"DOC_LINT_{'FINDINGS' if findings else 'OK'} documents={len(docs)} findings={len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
