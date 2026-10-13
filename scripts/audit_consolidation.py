# SPDX-License-Identifier: AGPL-3.0-only
"""Scoped static provenance and disclosure audit; never prints matched values."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "runtime/artifacts/reports/consolidation"


PATTERNS = {
    "credential_token": re.compile(r"\b(?:sk-|ghp_|xoxb-)[A-Za-z0-9_-]{16,}"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credential_url": re.compile(r"://[^/\s:@]+:(?P<secret>[^@\s/]+)@"),
    "email_address": re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),
}
# A credential URL whose secret component is only a redaction mask carries no
# secret (for example ``pados:***@host`` from safe_database_url). Anything else
# in the secret position, including partially masked values, is still flagged.
REDACTED_SECRET = re.compile(r"\*{3,}|\[REDACTED[^\]]*\]|<redacted>", re.IGNORECASE)


def line_findings(line: str) -> list[str]:
    names = []
    for name, pattern in PATTERNS.items():
        matches = list(pattern.finditer(line))
        if name == "credential_url":
            matches = [m for m in matches if not REDACTED_SECRET.fullmatch(m.group("secret"))]
        if matches:
            names.append(name)
    return names


def main() -> int:
    tracked = subprocess.check_output(["git", "diff", "--name-only", "-z"], cwd=ROOT).split(b"\0")
    added = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=ROOT).split(b"\0")
    paths = sorted({p.decode() for p in tracked + added if p})
    findings = []
    hashes = {}
    for relative in paths:
        path = ROOT / relative
        if not path.is_file() or relative.endswith("/audit.json"):
            continue
        if path.suffix not in {".py", ".ts", ".tsx", ".mjs", ".md", ".json", ".log", ".exit"}:
            continue
        raw = path.read_bytes()
        hashes[relative] = hashlib.sha256(raw).hexdigest()
        text = raw.decode("utf-8")
        for line_number, line in enumerate(text.splitlines(), 1):
            for name in line_findings(line):
                findings.append({"path": relative, "line": line_number, "rule": name})
    manifest = json.loads((ROOT / "docs/migration/PADOS_ASSET_MIGRATION_MANIFEST.json").read_text())
    allowed = {"DIRECT_PORT", "DIRECT_PORT_WITH_ADAPTER", "PATTERN_ONLY", "KNOWLEDGE_ONLY", "REJECT", "ALREADY_EXISTS_IN_PADOS"}
    required = {"source_repo", "source_commit", "source_path", "destination", "migration_type", "authority_owner", "adaptation_summary", "tests", "status"}
    for component in manifest["components"]:
        assert required <= component.keys()
        assert component["migration_type"] in allowed
        assert component["source_commit"] == manifest["donor_revisions"][component["source_repo"]]
    for relative in ("src/pados/execution/context.py", "src/pados/execution/receipts.py", "src/pados/execution/handoff.py", "src/pados/knowledge/identity.py"):
        tree = ast.parse((ROOT / relative).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(("sqlalchemy", "pados.memory", "pados.state_proof"))
    result = {"scope": "Changed/new implementation, tests, documentation and retained task logs; donor/private data excluded",
              "limitations": "Pattern scan is not a comprehensive private-data or secret detector",
              "findings": findings, "manifest_components": len(manifest["components"]),
              "execution_boundary_has_no_semantic_database_imports": True, "sha256": hashes}
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else REPORTS / "audit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Scanned {len(hashes)} files; findings={len(findings)}; classified components={len(manifest['components'])}")
    for finding in findings:
        print(json.dumps(finding))
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
