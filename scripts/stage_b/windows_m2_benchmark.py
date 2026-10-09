# SPDX-License-Identifier: AGPL-3.0-only
"""Stage B M2 Windows live run: pull the ONE approved model, verify its digest, benchmark it, archive raw evidence.

Run from a checkout of the Stage B branch (NOT C:\\PADOS) with the checkout's own virtual environment:

    .venv\\Scripts\\python.exe scripts\\stage_b\\windows_m2_benchmark.py --expected-commit <sha> --expected-tree <sha> --pull

Without ``--pull`` the script only benchmarks a model that is already present with the audited identity. The model,
tag, manifest digest and layer digests are the M1 selection and cannot be changed by arguments. The script refuses to
measure an artifact whose runtime-reported digest, local manifest bytes or layer list differ from the audited ones,
and records a failed download as ``PULL_FAILED``, not as a digest mismatch. Weights stay in the runtime's own store;
nothing here copies them, and the evidence archive contains no weights, credentials or hostname.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path

APPROVED_MODEL = "qwen3:8b"
APPROVED_MANIFEST_DIGEST = "sha256:500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41"
APPROVED_LAYERS = (
    "sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f",  # model, 5,225,374,496 bytes
    "sha256:ae370d884f108d16e7cc8fd5259ebc5773a0afa6e078b11f4ed7e39a27e0dfc4",  # template
    "sha256:d18a5cc71b84bc4af394a31116bd3932b42241de70c77d2b76d69a314ec8aa12",  # license
    "sha256:cff3f395ef3756ab63e58b0ad1b32bb6f802905cae1472e6a12034e4246fbbdb",  # params
)
PROTECTED = Path(r"C:\PADOS")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,  # noqa: S603,S607
                          check=True, text=True).stdout.strip()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _disk(path: Path) -> dict[str, int]:
    usage = shutil.disk_usage(path)
    return {"total_mib": usage.total // 2**20, "free_mib": usage.free // 2**20}


def _models_dir() -> Path:
    configured = os.environ.get("OLLAMA_MODELS")
    return Path(configured) if configured else Path.home() / ".ollama" / "models"


def _blob_state(models_dir: Path) -> dict[str, dict[str, int | None]]:
    """Sizes of each pinned layer's complete blob and of any partial download files, without reading the weights."""
    blobs = models_dir / "blobs"
    state: dict[str, dict[str, int | None]] = {}
    for layer in APPROVED_LAYERS:
        name = layer.replace(":", "-")
        complete = blobs / name
        partial = [p.stat().st_size for p in blobs.glob(f"{name}-partial*") if p.is_file()] if blobs.is_dir() else []
        state[layer] = {"complete_bytes": complete.stat().st_size if complete.is_file() else None,
                        "partial_files": len(partial), "partial_bytes": sum(partial)}
    return state


def _capture(py: str, repo: Path, out: Path, ollama_url: str, log: Path) -> int:
    completed = subprocess.run([py, "-m", "pados.local_intelligence.capture", "--out", str(out), "--ollama-url",  # noqa: S603
                                ollama_url, "--resource-id", "windows-live-device", "--disk-path", str(repo)], cwd=repo,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    log.write_bytes(completed.stdout)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--expected-tree", required=True)
    parser.add_argument("--pull", action="store_true", help="explicitly authorise pulling the approved model")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--out", type=Path, default=Path.home() / "pados-stage-b-evidence" /
                        f"m2-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    out: Path = args.out.resolve()
    if str(repo).casefold().startswith(str(PROTECTED).casefold()) or _inside(out, repo) \
            or str(out).casefold().startswith(str(PROTECTED).casefold()):
        print("REFUSED: checkout inside C:\\PADOS, or --out inside the repository / C:\\PADOS")
        return 2
    head, tree = _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD^{tree}")
    dirty = bool(_git(repo, "status", "--porcelain", "--untracked-files=no"))
    identity = {"commit": head, "tree": tree, "expected_commit": args.expected_commit, "expected_tree": args.expected_tree,
                "tracked_changes": dirty,
                "parity": "PASS" if (head, tree, dirty) == (args.expected_commit, args.expected_tree, False) else "FAIL"}
    if identity["parity"] != "PASS":
        print(json.dumps(identity, indent=2))
        print("REFUSED: checkout does not match the expected Stage B commit/tree")
        return 2
    out.mkdir(parents=True, exist_ok=False)
    (out / "logs").mkdir()
    py = sys.executable
    sys.path.insert(0, str(repo / "src"))
    from pados.local_intelligence.bench_summary import load, summarize
    from pados.local_intelligence.benchmark import (
        BenchmarkRefused,
        BenchmarkRunner,
        OllamaClient,
        PinnedArtifact,
        local_manifest_path,
        pull_and_verify,
        verify_present,
    )

    raw_path = out / "BENCHMARK_RAW.jsonl"

    def record(entry: dict[str, object]) -> None:
        with raw_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": time.time(), **entry}, ensure_ascii=False, default=str) + "\n")

    result: dict[str, object] = {"schema": "pados.stage-b.windows-m2-result.v1", "execution": "EXECUTED_WINDOWS_NATIVE",
                                 "identity": identity, "approved_model": APPROVED_MODEL,
                                 "approved_manifest_digest": APPROVED_MANIFEST_DIGEST,
                                 "approved_layers": list(APPROVED_LAYERS),
                                 "environment": {"platform": platform.platform(), "python": platform.python_version()},
                                 "started_at": datetime.now(UTC).isoformat()}
    result["capture_before_exit"] = _capture(py, repo, out / "capture_before", args.ollama_url, out / "logs" / "capture_before.log")
    models_dir = _models_dir()
    disk_root = models_dir if models_dir.exists() else Path.home()
    result["disk_before"] = _disk(disk_root)
    result["blobs_before"] = _blob_state(models_dir)
    pin = PinnedArtifact(APPROVED_MODEL, APPROVED_MANIFEST_DIGEST, APPROVED_LAYERS)
    manifest_file = local_manifest_path(models_dir, APPROVED_MODEL)
    result["local_manifest_path"] = str(manifest_file.relative_to(models_dir))

    def read_manifest() -> bytes | None:
        return manifest_file.read_bytes() if manifest_file.is_file() else None

    client = OllamaClient(args.ollama_url)
    status = "NOT_STARTED"
    try:
        present = verify_present(client, pin, read_manifest)
        if present["identity_refusal"] == "MODEL_ABSENT":
            if not args.pull:
                raise BenchmarkRefused("MODEL_ABSENT_AND_PULL_NOT_AUTHORISED")
            result["pull"] = pull_and_verify(client, pin, record, read_manifest)
        else:
            record({"record_type": "pull", "model": APPROVED_MODEL, "skipped": "ALREADY_PRESENT", **present})
            if present["identity_refusal"]:
                raise BenchmarkRefused(f"PRESENT_{present['identity_refusal']}")
        result["disk_after_pull"] = _disk(disk_root)
        BenchmarkRunner(client=client, model=APPROVED_MODEL, record=record).run_all()
        status = "COMPLETED"
    except BenchmarkRefused as exc:
        status = f"REFUSED:{exc.code}"
    result["status"] = status
    result["capture_after_exit"] = _capture(py, repo, out / "capture_after", args.ollama_url, out / "logs" / "capture_after.log")
    result["disk_after"] = _disk(disk_root)
    result["blobs_after"] = _blob_state(models_dir)
    before, after = result["disk_before"], result["disk_after"]
    result["disk_impact_mib"] = before["free_mib"] - after["free_mib"]  # type: ignore[index]
    if raw_path.exists():
        summary = summarize(load(raw_path))
        (out / "BENCHMARK_SUMMARY.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    result["finished_at"] = datetime.now(UTC).isoformat()
    (out / "WINDOWS_M2_RESULT.json").write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {str(p.relative_to(out)).replace(chr(92), '/')}"
            for p in sorted(p for p in out.rglob("*") if p.is_file())]
    (out / "SHA256SUMS").write_text("\n".join(sums) + "\n", encoding="utf-8")
    archive = out.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(p for p in out.rglob("*") if p.is_file()):
            bundle.write(path, f"{out.name}/{str(path.relative_to(out)).replace(chr(92), '/')}")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(json.dumps({"status": status, "identity": identity["parity"], "disk_impact_mib": result["disk_impact_mib"]}))
    print(f"EVIDENCE_ARCHIVE={archive}")
    print(f"EVIDENCE_ARCHIVE_SHA256={digest}")
    return 0 if status == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
