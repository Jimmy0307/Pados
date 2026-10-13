# SPDX-License-Identifier: AGPL-3.0-only
"""Record reproducible consolidation checks without reading runtime secrets."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "runtime/artifacts/reports/consolidation"


def main() -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONPYCACHEPREFIX": str(ROOT / ".migration/pycache")}
    checks = [
        ("compile", [sys.executable, "-m", "compileall", "-q", "src", "tests/consolidation", "tests/unit/test_consolidation_product_loop.py", "scripts/validate_consolidation.py"], ROOT),
        ("boundaries", [sys.executable, "-m", "unittest", "discover", "-s", "tests/consolidation", "-v"], ROOT),
        ("backend", [sys.executable, "-m", "pytest", "tests/unit", "tests/security", "tests/integration"], ROOT),
        ("static", [sys.executable, "-m", "ruff", "check", "src/pados/execution", "src/pados/knowledge"], ROOT),
        ("types", [sys.executable, "-m", "mypy", "src/pados/execution", "src/pados/knowledge"], ROOT),
        ("frontend_cache", ["node", "web/src/resourceCache.node-test.mjs"], ROOT),
        ("frontend_typecheck", ["npm", "run", "typecheck"], ROOT / "web"),
        ("frontend_tests", ["npm", "test"], ROOT / "web"),
        ("frontend_lint", ["npm", "run", "lint"], ROOT / "web"),
        ("frontend_build", ["npm", "run", "build"], ROOT / "web"),
        ("api_contract", [sys.executable, "-c", "from pados.api.app import app; schema = app.openapi(); assert schema['paths']; print(len(schema['paths']))"], ROOT),
        ("doctor", [sys.executable, "-m", "pados.cli.main", "doctor", "--json"], ROOT),
        ("startup", [sys.executable, "-m", "uvicorn", "pados.api.app:app", "--host", "127.0.0.1", "--port", "18991"], ROOT),
        ("disclosure_audit", [sys.executable, "scripts/audit_consolidation.py"], ROOT),
        ("diff", ["git", "diff", "--check"], ROOT),
    ]
    results = []
    for name, command, cwd in checks:
        try:
            completed = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=45)
            code, output = completed.returncode, completed.stdout + completed.stderr
        except subprocess.TimeoutExpired:
            code, output = 124, "CHECK_TIMEOUT; completion not proven\n"
        except OSError as exc:
            code, output = 127, f"CHECK_UNAVAILABLE: {type(exc).__name__}\n"
        path = REPORTS / f"{name}.log"
        path.write_text(output)
        results.append({"name": name, "command": command, "cwd": str(cwd.relative_to(ROOT)) or ".",
                        "exit_code": code, "log_path": str(path.relative_to(ROOT))})
        print(f"{name}: exit={code} log={path.relative_to(ROOT)}", flush=True)
    (REPORTS / "checks.json").write_text(json.dumps(results, indent=2) + "\n")
    dependencies = {name: importlib.util.find_spec(name) is not None for name in
                    ("pytest", "pydantic", "sqlalchemy", "fastapi", "psycopg", "uvicorn", "ruff", "mypy")}
    (REPORTS / "preflight.json").write_text(json.dumps({"dependencies": dependencies,
        "frontend_dependencies_installed": (ROOT / "web/node_modules").is_dir(),
        "live_codex": "LIVE_CODEX_UAT_PENDING"}, indent=2) + "\n")
    return int(any(row["exit_code"] for row in results))


if __name__ == "__main__":
    raise SystemExit(main())
