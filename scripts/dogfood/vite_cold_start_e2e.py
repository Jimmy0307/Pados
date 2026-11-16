# SPDX-License-Identifier: AGPL-3.0-only
"""Cold-start pairing E2E: the first page load of a fresh Vite dev server pairs a browser, N times in a row.

Each iteration starts from nothing warm: ``pados serve`` is (re)started, ``web/node_modules/.vite`` (Vite's dependency
optimizer cache) is removed, a Vite dev server is started on a free port with its log captured, the driver waits for
the startup readiness condition, then ``web/e2e/pairing-cold-start-live.spec.ts`` pairs on the very first page load
and asserts that no reload, navigation or failed identity request happened while the single-use code was in flight.

The backend is the shared bootstrap of scripts/dogfood/identity_e2e.py (``pados init``, a private loopback
PostgreSQL with ``--pg-bin``, the lane migrations, identity sessions on). Loopback only; never the user's ports.

Evidence: ``<out>/VITE_COLD_START_E2E_RESULT.json`` (``pados.vite-cold-start-e2e.v1``), per-iteration Vite, API and
Playwright logs, the spec's event timelines and Playwright traces. Exit 0 only when every iteration passed.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import identity_e2e as identity  # noqa: E402 - shared bootstrap of the identity E2E
import local_dogfood_e2e as dogfood  # noqa: E402 - shared helpers of the dogfood E2E
import web_readiness  # noqa: E402 - the dev server startup readiness condition

REPO = dogfood.REPO
WEB = REPO / "web"
CONTRACT = "pados.vite-cold-start-e2e.v1"
SPEC = "e2e/pairing-cold-start-live.spec.ts"
RESERVED_PORTS = {5173, 8088, 5432}  # the operator's own PADOS: never used by a test run


def start_vite(log_path: Path, web_port: int, api_port: int) -> tuple[subprocess.Popen[str], object]:
    """A Vite dev server on ``web_port`` proxying to the API, with the optimizer's debug log captured."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PADOS_")}
    env.update({"PADOS_WEB_PORT": str(web_port), "PADOS_WEB_BACKEND_URL": f"http://127.0.0.1:{api_port}",
                "DEBUG": "vite:deps", "NO_COLOR": "1", "FORCE_COLOR": "0"})
    log = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - lives as long as the dev server
    argv = ["node", "node_modules/vite/bin/vite.js", "--host", "127.0.0.1", "--port", str(web_port), "--strictPort"]
    vite = subprocess.Popen(argv, cwd=WEB, env=env, stdout=log, stderr=subprocess.STDOUT, text=True,
                            stdin=subprocess.DEVNULL)
    return vite, log


def stop_vite(vite: subprocess.Popen[str], log: object) -> None:
    vite.terminate()
    try:
        vite.wait(timeout=30)
    except subprocess.TimeoutExpired:
        vite.kill()
        vite.wait(timeout=30)
    with contextlib.suppress(OSError):
        log.close()  # type: ignore[attr-defined]


def run_spec(args: argparse.Namespace, out: Path, root: Path, iteration: int) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PADOS_") and k != "CI"}  # reuse the started server
    env.update({"PADOS_WEB_PORT": str(args.web_port), "PADOS_WEB_BACKEND_URL": f"http://127.0.0.1:{args.api_port}",
                "PADOS_E2E_COLD_START_LIVE": "1", "PADOS_E2E_PYTHON": sys.executable,
                "PADOS_E2E_API_URL": f"http://127.0.0.1:{args.api_port}", "PADOS_E2E_ITERATION": str(iteration),
                "PADOS_E2E_TOKEN_FILE": str(root / "runtime" / "local" / "control.token"),
                "PADOS_E2E_EVIDENCE_DIR": str(out), "PYTHONUTF8": "1"})
    argv = ["npx", "playwright", "test", SPEC, "--reporter=list", "--workers=1", "--retries=0", "--trace=on",
            "--output", str(out / "playwright-results")]
    return subprocess.run(argv, cwd=WEB, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=600, shell=os.name == "nt")


def iteration_run(args: argparse.Namespace, run: dogfood.Run, root: Path, env: dict[str, str], i: int) -> bool:
    out = run.out / f"iter-{i:02d}"
    out.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(WEB / "node_modules" / ".vite", ignore_errors=True)  # cold optimizer: nothing pre-bundled
    server = dogfood.start_server(run, env, args.api_port, f"iter-{i:02d}/serve")
    vite, log = start_vite(out / "vite.log", args.web_port, args.api_port)
    started = time.monotonic()
    try:
        ready = web_readiness.wait_ready(f"http://127.0.0.1:{args.web_port}", f"http://127.0.0.1:{args.api_port}",
                                         mode=args.readiness, deadline_s=120.0, alive=lambda: vite.poll() is None)
        ready_s = round(time.monotonic() - started, 2)
        result = run_spec(args, out, root, i)
    finally:
        stop_vite(vite, log)
        dogfood.stop_server(server)
    (out / "playwright.log").write_text(f"exit={result.returncode}\n--- stdout\n{result.stdout}\n--- stderr\n"
                                        f"{result.stderr}", encoding="utf-8")
    vite_log = (out / "vite.log").read_text("utf-8", "replace")
    reloads = re.findall(r"new dependencies optimized[^\n]*|optimized dependencies changed[^\n]*|page reload[^\n]*",
                         vite_log)
    events_file = out / f"cold-start-events-{i}.json"
    events = json.loads(events_file.read_text("utf-8")) if events_file.is_file() else []
    passed = result.returncode == 0 and bool(re.search(r"\b1 passed\b", result.stdout)) and not reloads
    return run.record(f"cold_start_pairing_{i:02d}", passed, exit=result.returncode, readiness=args.readiness,
                      ready_seconds=ready_s, ready=ready, vite_reload_lines=reloads,
                      navigations=[e for e in events if e["kind"] == "navigated"],
                      vite_messages=[e for e in events if e["kind"] == "vite-ws"],
                      pair=[e for e in events if e["kind"].startswith("pair") or e["kind"] == "identity-failed"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pg-bin", type=Path, help="native PostgreSQL bin dir: start a private loopback cluster")
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=55490)
    parser.add_argument("--pg-user", default="pados")
    parser.add_argument("--api-port", type=int, default=0, help="0: a free port")
    parser.add_argument("--web-port", type=int, default=0, help="0: a free port")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--readiness", choices=web_readiness.MODES, default="full",
                        help="'full' (default): the startup readiness condition; 'html': the old HTTP-200-on-/ check")
    args = parser.parse_args(argv)
    args.api_port = args.api_port or identity.free_port()
    args.web_port = args.web_port or identity.free_port()
    if RESERVED_PORTS & {args.api_port, args.web_port, args.pg_port}:
        parser.error(f"ports {sorted(RESERVED_PORTS)} belong to the operator's own PADOS; choose others")
    run = dogfood.Run(args.out.resolve())
    root = Path(tempfile.mkdtemp(prefix="pados-vite-cold-"))
    db_pass = "" if args.pg_bin else os.environ.get("PADOS_DB_PASSWORD", "")
    cluster = dogfood.Cluster(args.pg_bin, root, args.pg_port) if args.pg_bin else None
    started = time.time()
    try:
        if cluster:
            cluster.start()
        env = identity.prepare(args, run, root, db_pass)
        for i in range(1, args.iterations + 1):
            iteration_run(args, run, root, env, i)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded as evidence, then the run fails
        run.record("unexpected_failure", False, error=f"{type(exc).__name__}: {exc}"[:800])
    finally:
        if cluster:
            cluster.stop()
        with contextlib.suppress(OSError):
            shutil.rmtree(root, ignore_errors=True)
    cold = [s for s in run.steps if s["step"].startswith("cold_start_pairing_")]
    passed = bool(cold) and not any(step["status"] == "FAIL" for step in run.steps)
    counts = {"iterations": len(cold), "passed": sum(s["status"] == "PASS" for s in cold),
              "failed": sum(s["status"] == "FAIL" for s in cold)}
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    (run.out / "VITE_COLD_START_E2E_RESULT.json").write_text(json.dumps({
        "runner_contract": CONTRACT, "verdict": "PASS" if passed else "FAIL", "commit": head,
        "platform": platform.platform(), "python": sys.version.split()[0], "seconds": int(time.time() - started),
        "finished": datetime.now(UTC).isoformat(), "readiness": args.readiness, "counts": counts,
        "steps": run.steps}, indent=2, default=str), encoding="utf-8")
    print(f"VITE COLD START E2E {'PASS' if passed else 'FAIL'} {counts} -> {run.out}", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
