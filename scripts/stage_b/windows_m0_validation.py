# SPDX-License-Identifier: AGPL-3.0-only
"""Stage B M0 Windows live validation: machine-captured evidence, archived with SHA-256 for transfer.

Run from a checkout of the Stage B branch (NOT C:\\PADOS) with the checkout's own virtual environment:

    .venv\\Scripts\\python.exe scripts\\stage_b\\windows_m0_validation.py --expected-commit <sha> --expected-tree <sha>

Standard library only; every command is an argv list (no shell, no PowerShell), and each gate's result is the
child process exit code. Nothing is downloaded, installed, pushed or published. The output directory (outside
the repository) receives the logs, the device/runtime capture with its raw probe outputs, WINDOWS_M0_RESULT.json,
SHA256SUMS and a zip of all of it plus the zip's own .sha256 for verification after transfer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

PROTECTED = Path(r"C:\PADOS")
PYTEST = ("-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider", "-rfEs", "--tb=short")
QUEUE_TESTS = ("tests/compute_node/test_node_service_backends.py::test_resource_bounds_fail_closed",
               "tests/compute_node/test_node_queue_admission.py")


def _run(argv: list[str], cwd: Path, log: Path, env: dict[str, str] | None = None, timeout: int = 3600) -> dict[str, object]:
    started = time.monotonic()
    try:
        completed = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,  # noqa: S603
                                   stderr=subprocess.STDOUT, timeout=timeout, check=False,
                                   env={**os.environ, **(env or {})})
        output, code = completed.stdout, completed.returncode
    except subprocess.TimeoutExpired as exc:
        output, code = (exc.stdout or b"") + b"\nGATE_TIMEOUT\n", 124
    except OSError as exc:
        output, code = f"GATE_COULD_NOT_START: {type(exc).__name__}\n".encode(), 127
    log.write_bytes(output)
    lines = [line for line in output.decode("utf-8", errors="replace").splitlines() if line.strip()]
    return {"exit_code": code, "summary": lines[-1][:300] if lines else "", "log": log.name,
            "duration_seconds": round(time.monotonic() - started, 1), "argv": [Path(argv[0]).name, *argv[1:]]}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,  # noqa: S603,S607
                          check=True, text=True).stdout.strip()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--out", type=Path, default=Path.home() / "pados-stage-b-evidence" /
                        f"m0-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--queue-runs", type=int, default=10)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    if str(repo).casefold().startswith(str(PROTECTED).casefold()):
        print("REFUSED: this checkout is inside the protected C:\\PADOS repository")
        return 2
    out: Path = args.out.resolve()
    if _inside(out, repo) or str(out).casefold().startswith(str(PROTECTED).casefold()):
        print("REFUSED: --out must be outside the repository and outside C:\\PADOS")
        return 2
    out.mkdir(parents=True, exist_ok=False)
    logs = out / "logs"
    logs.mkdir()
    py = sys.executable
    # Per-gate pytest temp roots outside the evidence directory: pytest's shared "pytest-current" link under the
    # user temp dir failed with WinError 5 on the first Windows run (m0-20261007T085245Z).
    basetemp = Path(tempfile.gettempdir()) / f"pados-stage-b-{out.name}"
    basetemp.mkdir(parents=True, exist_ok=True)  # pytest creates --basetemp itself, but not its parent

    def pytest_args(gate: str) -> list[str]:
        return [py, *PYTEST, f"--basetemp={basetemp / gate}"]

    head, tree = _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD^{tree}")
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no")
    identity = {"commit": head, "tree": tree, "expected_commit": args.expected_commit, "expected_tree": args.expected_tree,
                "tracked_changes": bool(dirty)}
    identity["parity"] = "PASS" if (head == args.expected_commit and tree == args.expected_tree and not dirty) else "FAIL"
    clocks = {}
    for name in ("monotonic", "perf_counter", "time"):
        info = time.get_clock_info(name)
        clocks[name] = {"implementation": info.implementation, "resolution": info.resolution}
    environment = {"platform": platform.platform(), "python": platform.python_version(), "machine": platform.machine(),
                   "executable_in_repo_venv": _inside(Path(py), repo), "clocks": clocks}
    gates: dict[str, dict[str, object]] = {}
    if identity["parity"] != "PASS":
        print(json.dumps(identity, indent=2))
        print("REFUSED: checkout does not match the expected Stage B commit/tree (or has tracked changes)")
    else:
        gates["M0W00-pip-freeze"] = _run([py, "-m", "pip", "freeze", "--exclude-editable"], repo, logs / "M0W00-pip-freeze.log")
        gates["M0W01-ruff"] = _run([py, "-m", "ruff", "check", "src", "tests"], repo, logs / "M0W01-ruff.log")
        gates["M0W02-mypy"] = _run([py, "-m", "mypy", "src"], repo, logs / "M0W02-mypy.log")
        gates["M0W03-capture"] = _run([py, "-m", "pados.local_intelligence.capture", "--out", str(out / "capture"),
                                       "--ollama-url", args.ollama_url, "--resource-id", "windows-live-device",
                                       "--disk-path", str(repo)], repo, logs / "M0W03-capture.log")
        gates["M0W04-local-intelligence-tests"] = _run([*pytest_args("M0W04"), "tests/local_intelligence"], repo,
                                                       logs / "M0W04-local-intelligence-tests.log")
        gates["M0W05-compute-node-tests"] = _run([*pytest_args("M0W05"), "tests/compute_node"], repo, logs / "M0W05-compute-node-tests.log")
        gates["M0W06-boundary-tests"] = _run([*pytest_args("M0W06"), "tests/resources", "tests/capability/test_fabric_boundaries.py",
                                             "tests/learning/test_boundary.py"], repo, logs / "M0W06-boundary-tests.log")
        runs = []
        for index in range(1, args.queue_runs + 1):
            runs.append(_run([*pytest_args(f"M0W07-{index:02d}"), *QUEUE_TESTS], repo, logs / f"M0W07-queue-run-{index:02d}.log"))
        gates["M0W07-queue-backpressure-repeated"] = {
            "exit_code": 0 if all(r["exit_code"] == 0 for r in runs) else 1,
            "summary": f"{sum(r['exit_code'] == 0 for r in runs)} PASS / {len(runs)} runs", "runs": runs}
    capture_dir = out / "capture"
    result = {
        "schema": "pados.stage-b.windows-m0-result.v1", "recorded_at": datetime.now(UTC).isoformat(),
        "execution": "EXECUTED_WINDOWS_NATIVE", "identity": identity, "environment": environment, "gates": gates,
        "capture_files": sorted(str(p.relative_to(out)).replace("\\", "/") for p in capture_dir.rglob("*") if p.is_file())
        if capture_dir.exists() else [],
    }
    (out / "WINDOWS_M0_RESULT.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sums = []
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        sums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {str(path.relative_to(out)).replace(chr(92), '/')}")
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    archive = out.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(p for p in out.rglob("*") if p.is_file()):
            bundle.write(path, f"{out.name}/{str(path.relative_to(out)).replace(chr(92), '/')}")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(json.dumps({"identity": identity, "gates": {k: (v["exit_code"], v["summary"]) for k, v in gates.items()}},
                     indent=2))
    print(f"EVIDENCE_ARCHIVE={archive}")
    print(f"EVIDENCE_ARCHIVE_SHA256={digest}")
    failed = identity["parity"] != "PASS" or any(v["exit_code"] != 0 for v in gates.values())
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
