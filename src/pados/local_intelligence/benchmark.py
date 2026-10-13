# SPDX-License-Identifier: AGPL-3.0-only
"""Repeatable local-model benchmark against one loopback runtime (measurement only).

The runner pulls nothing unless asked, verifies the pulled artifact against the audited manifest digest before
measuring anything, and writes one JSON record per observation. Model outputs are kept as evidence and checked by
deterministic checkers; they never become authority. Resource figures come from ``nvidia-smi`` and ``psutil``
samples taken while requests run; where a figure cannot be measured it is recorded as unavailable, not guessed.
"""
from __future__ import annotations

import hashlib
import json
import re
import statistics
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import psutil  # type: ignore[import-untyped]  # declared runtime dependency; no stubs are pinned

from pados.local_intelligence.bench_tasks import (
    BENCHMARK_VERSION,
    DETERMINISM_PROMPT,
    BenchTask,
    long_context_tasks,
    quality_tasks,
    task_set_digest,
    text,
)
from pados.local_intelligence.probes import CommandNotFound, CommandTimedOut, run_command
from pados.local_intelligence.runtimes import loopback_endpoint

MAX_OUTPUT_CHARS = 8000
SEED = 42


class BenchmarkRefused(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TotalDeadlineExceeded(RuntimeError):
    """The WHOLE request (connect + first token + generation) exceeded its deadline; the client closed the
    connection. Whether the runtime stopped generating server-side is NOT implied (not observable here)."""

    def __init__(self, elapsed_s: float, first_token_s: float | None, chunks: int) -> None:
        super().__init__("TOTAL_DEADLINE_EXCEEDED")
        self.elapsed_s = elapsed_s
        self.first_token_s = first_token_s
        self.chunks = chunks


CONNECT_TIMEOUT_S = 5.0


class OllamaClient:
    """Loopback-only measurement client for Ollama's native API (no proxies, no credentials)."""

    def __init__(self, endpoint: str, *, timeout: float = 600.0) -> None:
        if loopback_endpoint(endpoint) is None:
            raise BenchmarkRefused("BENCHMARK_LOOPBACK_ONLY")
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def _client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(base_url=self.endpoint, trust_env=False, timeout=timeout or self.timeout)

    def get(self, path: str) -> dict[str, Any]:
        with self._client(10.0) as client:
            response = client.get(path)
            response.raise_for_status()
            body = response.json()
        return body if isinstance(body, dict) else {}

    def stream(self, path: str, body: dict[str, Any], *, timeout: float | None = None,
               deadline_s: float | None = None) -> Iterator[dict[str, Any]]:
        """Stream NDJSON. ``timeout`` is httpx's per-operation timeout (connect/read/write each); ``deadline_s`` is a
        TOTAL request deadline enforced by a watchdog that closes the response, so a stream that keeps sending
        chunks can still not outlive it (the per-read timeout alone never fires while chunks keep arriving)."""
        if deadline_s is None:
            with self._client(timeout) as client, client.stream("POST", path, json=body) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.strip():
                        yield json.loads(line)
            return
        started = time.perf_counter()
        expired = threading.Event()
        limits = httpx.Timeout(deadline_s, connect=min(CONNECT_TIMEOUT_S, deadline_s))
        with httpx.Client(base_url=self.endpoint, trust_env=False, timeout=limits) as client:
            with client.stream("POST", path, json=body) as response:

                def expire() -> None:
                    expired.set()
                    response.close()  # client-side cancellation: the connection is closed

                watchdog = threading.Timer(max(0.0, deadline_s - (time.perf_counter() - started)), expire)
                watchdog.daemon = True
                watchdog.start()
                try:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if expired.is_set() or time.perf_counter() - started > deadline_s:
                            expired.set()
                            break
                        if line.strip():
                            yield json.loads(line)
                except (httpx.HTTPError, RuntimeError):
                    if not expired.is_set():
                        raise
                finally:
                    watchdog.cancel()
        if expired.is_set():
            raise TotalDeadlineExceeded(time.perf_counter() - started, None, -1)

    def unload(self, model: str) -> None:
        with self._client(60.0) as client:
            client.post("/api/generate", json={"model": model, "keep_alive": 0}).raise_for_status()

    def chat(self, model: str, task_messages: tuple[dict[str, str], ...], *, num_ctx: int, num_predict: int,
             think: bool, format: dict[str, Any] | None = None, tools: tuple[dict[str, Any], ...] = (),
             timeout: float | None = None, keep_alive: str = "10m", deadline_s: float | None = None) -> dict[str, Any]:
        """One streamed chat call; returns content, tool calls, TTFT and Ollama's own counters."""
        body: dict[str, Any] = {"model": model, "messages": list(task_messages), "stream": True, "think": think,
                                "keep_alive": keep_alive,
                                "options": {"temperature": 0, "seed": SEED, "num_ctx": num_ctx, "num_predict": num_predict}}
        if format is not None:
            body["format"] = format
        if tools:
            body["tools"] = list(tools)
        started = time.perf_counter()
        first: float | None = None
        content: list[str] = []
        thinking: list[str] = []
        tool_calls: list[Any] = []
        final: dict[str, Any] = {}
        chunks = 0
        try:
            for chunk in self.stream("/api/chat", body, timeout=timeout, deadline_s=deadline_s):
                chunks += 1
                message = chunk.get("message") or {}
                piece = message.get("content") or ""
                reasoning = message.get("thinking") or ""
                if (piece or reasoning or message.get("tool_calls")) and first is None:
                    first = time.perf_counter()
                content.append(piece)
                thinking.append(reasoning)
                tool_calls.extend(message.get("tool_calls") or [])
                if chunk.get("done"):
                    final = chunk
        except TotalDeadlineExceeded:
            raise TotalDeadlineExceeded(time.perf_counter() - started, (first - started) if first else None,
                                        chunks) from None
        ended = time.perf_counter()
        elapsed = ended - started
        counters = {key: final.get(key) for key in ("total_duration", "load_duration", "prompt_eval_count",
                                                     "prompt_eval_duration", "eval_count", "eval_duration")}
        return {"content": "".join(content), "thinking_chars": len("".join(thinking)), "tool_calls": tool_calls,
                "done_reason": final.get("done_reason"), "ttft_s": (first - started) if first else None,
                "wall_s": elapsed, "t_start": started, "t_first": first, "t_end": ended, **counters}


@dataclass
class ResourceSampler:
    """Samples GPU memory/utilisation (nvidia-smi), system CPU and the runtime processes' RSS while active."""

    interval: float = 0.5
    process_names: tuple[str, ...] = ("ollama",)
    samples: list[dict[str, Any]] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def __enter__(self) -> ResourceSampler:
        self.samples = []
        self._stop.clear()
        psutil.cpu_percent(interval=None)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.samples.append(self.sample())
            self._stop.wait(self.interval)

    def sample(self) -> dict[str, Any]:
        gpu: dict[str, Any] = {}
        try:
            result = run_command(("nvidia-smi", "--query-gpu=memory.used,utilization.gpu",
                                  "--format=csv,noheader,nounits"), 5)
            fields = result.stdout.decode("utf-8", errors="replace").strip().splitlines()[0].split(",")
            gpu = {"gpu_mem_used_mib": int(fields[0].strip()), "gpu_util_pct": int(fields[1].strip())}
        except (CommandNotFound, CommandTimedOut, IndexError, ValueError):
            gpu = {"gpu_unavailable": True}
        rss = 0
        for process in psutil.process_iter(["name", "memory_info"]):
            name = (process.info.get("name") or "").lower()
            if any(item in name for item in self.process_names) and process.info.get("memory_info") is not None:
                rss += int(process.info["memory_info"].rss)
        memory = psutil.virtual_memory()
        return {"t": time.monotonic(), **gpu, "cpu_pct": psutil.cpu_percent(interval=None),
                "runtime_rss_mib": rss // 2**20, "sys_available_mib": int(memory.available) // 2**20}

    def summary(self) -> dict[str, Any]:
        def peak(key: str) -> Any:
            values = [s[key] for s in self.samples if key in s]
            return max(values) if values else None

        def mean(key: str) -> Any:
            values = [s[key] for s in self.samples if key in s]
            return round(statistics.fmean(values), 1) if values else None

        return {"samples": len(self.samples), "gpu_mem_used_peak_mib": peak("gpu_mem_used_mib"),
                "gpu_util_mean_pct": mean("gpu_util_pct"), "gpu_util_peak_pct": peak("gpu_util_pct"),
                "cpu_mean_pct": mean("cpu_pct"), "runtime_rss_peak_mib": peak("runtime_rss_mib"),
                "sys_available_min_mib": min((s["sys_available_mib"] for s in self.samples), default=None)}


Record = Callable[[dict[str, Any]], None]


def _rates(result: dict[str, Any]) -> dict[str, Any]:
    def per_second(count: Any, duration: Any) -> float | None:
        return round(count / (duration / 1e9), 2) if isinstance(count, int) and isinstance(duration, int) and duration else None

    return {"gen_tokens_per_s": per_second(result.get("eval_count"), result.get("eval_duration")),
            "prompt_tokens_per_s": per_second(result.get("prompt_eval_count"), result.get("prompt_eval_duration")),
            "load_s": round(result["load_duration"] / 1e9, 3) if isinstance(result.get("load_duration"), int) else None}


def _clip(result: dict[str, Any]) -> dict[str, Any]:
    content = str(result.get("content") or "")
    return {**result, "content": content[:MAX_OUTPUT_CHARS], "content_truncated": len(content) > MAX_OUTPUT_CHARS,
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}


@dataclass
class BenchmarkRunner:
    client: OllamaClient
    model: str
    record: Record
    sampler_factory: Callable[[], ResourceSampler] = ResourceSampler
    log: Callable[[str], None] = print

    def _call(self, phase: str, task_id: str, messages: tuple[dict[str, str], ...], **options: Any) -> dict[str, Any]:
        sampler = self.sampler_factory()
        started = datetime.now(UTC).isoformat()
        try:
            with sampler:
                result = self.client.chat(self.model, messages, **options)
            outcome = {"ok": True, **_clip(result), **_rates(result)}
        except TotalDeadlineExceeded as exc:
            outcome = {"ok": False, "error": "TOTAL_DEADLINE_EXCEEDED", "elapsed_s": round(exc.elapsed_s, 3),
                       "first_token_s": None if exc.first_token_s is None else round(exc.first_token_s, 3),
                       "chunks_before_deadline": exc.chunks, "client_cancellation": "CONNECTION_CLOSED_BY_CLIENT"}
        except httpx.TimeoutException as exc:
            outcome = {"ok": False, "error": "CONNECT_TIMEOUT" if isinstance(exc, httpx.ConnectTimeout)
                       else "CLIENT_TIMEOUT"}
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            outcome = {"ok": False, "error": f"{type(exc).__name__}"[:80]}
        entry = {"record_type": "call", "phase": phase, "task_id": task_id, "started_at": started,
                 "options": {k: v for k, v in options.items() if k not in {"format", "tools"}},
                 **outcome, "resources": sampler.summary()}
        self.record(entry)
        return entry

    def ps(self, phase: str) -> dict[str, Any]:
        try:
            loaded = self.client.get("/api/ps").get("models") or []
        except httpx.HTTPError as exc:
            loaded = [{"error": type(exc).__name__}]
        entry = {"record_type": "ps", "phase": phase, "models": loaded}
        self.record(entry)
        return entry

    # -- phases ---------------------------------------------------------------------------------------------------
    def latency(self, *, cold_runs: int = 3, warm_runs: int = 5) -> None:
        prompt = DETERMINISM_PROMPT
        for index in range(cold_runs):
            self.client.unload(self.model)
            self._call("cold_start", f"cold_{index + 1}", prompt, num_ctx=8192, num_predict=64, think=False)
        self.ps("after_cold")
        for index in range(warm_runs):
            self._call("warm_start", f"warm_{index + 1}", prompt, num_ctx=8192, num_predict=64, think=False)
        self.ps("after_warm")

    def context(self) -> None:
        """short -> ~8K -> ~16K; 16K is attempted only when every 8K call succeeded (never forced)."""
        for task in long_context_tasks():
            size = task.category.rsplit("_", 1)[-1]
            if size == "16k" and not self._context_ok:
                self.record({"record_type": "skip", "phase": "context", "task_id": task.task_id,
                             "reason": "8K_NOT_STABLE"})
                continue
            entry = self._quality(task, phase="context")
            if size == "8k" and not entry.get("ok"):
                self._context_ok = False
            self.ps(f"after_{task.task_id}")

    _context_ok: bool = True

    def _quality(self, task: BenchTask, *, phase: str = "quality") -> dict[str, Any]:
        entry = self._call(phase, task.task_id, task.messages, num_ctx=task.num_ctx, num_predict=task.num_predict,
                           think=task.think, format=task.format, tools=task.tools)
        if entry["ok"]:
            passed, details = task.check({"content": entry["content"], "tool_calls": entry.get("tool_calls")})
        else:
            passed, details = False, {"error": entry.get("error")}
        result = {"record_type": "check", "phase": phase, "task_id": task.task_id, "category": task.category,
                  "think": task.think, "passed": bool(passed), "details": details}
        self.record(result)
        return {**entry, "passed": passed}

    def quality(self) -> None:
        for task in quality_tasks():
            self._quality(task)

    def determinism(self, runs: int = 3) -> None:
        digests = []
        for index in range(runs):
            entry = self._call("determinism", f"determinism_{index + 1}", DETERMINISM_PROMPT, num_ctx=8192,
                               num_predict=96, think=False)
            digests.append(hashlib.sha256(text(entry).encode("utf-8")).hexdigest() if entry["ok"] else None)
        self.record({"record_type": "check", "phase": "determinism", "task_id": "determinism", "category": "determinism",
                     "think": False, "passed": len(set(digests)) == 1 and None not in digests,
                     "details": {"digests": digests}})

    def concurrency(self, levels: tuple[int, ...] = (2, 4)) -> None:
        for level in levels:
            barrier = threading.Barrier(level)

            def one(index: int, level: int = level, barrier: threading.Barrier = barrier) -> dict[str, Any]:
                barrier.wait(60)
                started = time.perf_counter()
                try:
                    result = self.client.chat(self.model, DETERMINISM_PROMPT, num_ctx=8192, num_predict=96, think=False)
                    return {"ok": True, "wall_s": time.perf_counter() - started, "ttft_s": result.get("ttft_s"),
                            "eval_count": result.get("eval_count"), "t_start": result.get("t_start"),
                            "t_first": result.get("t_first"), "t_end": result.get("t_end"), **_rates(result)}
                except httpx.HTTPError as exc:
                    return {"ok": False, "error": type(exc).__name__, "wall_s": time.perf_counter() - started}

            sampler = self.sampler_factory()
            started = time.perf_counter()
            with sampler, ThreadPoolExecutor(max_workers=level) as pool:
                results = list(pool.map(one, range(level)))
            wall = time.perf_counter() - started
            tokens = sum(r.get("eval_count") or 0 for r in results if r["ok"])
            for item in results:  # monotonic stamps relative to the level start (comparable within one level)
                for key in ("t_start", "t_first", "t_end"):
                    if isinstance(item.get(key), float):
                        item[key] = round(item[key] - started, 4)
            self.record({"record_type": "concurrency", "phase": "concurrency", "level": level, "results": results,
                         "wall_s": wall, "aggregate_tokens_per_s": round(tokens / wall, 2) if wall else None,
                         "errors": sum(1 for r in results if not r["ok"]), "resources": sampler.summary(),
                         "semantics": concurrency_semantics(results)})

    timeout_deadline_s: float = 2.0

    def timeout_behaviour(self) -> None:
        """Induce a TOTAL-deadline expiry on a long generation, then prove (or not) recovery.

        Valid only if the deadline actually expired (``TOTAL_DEADLINE_EXCEEDED``); a request that completed in time
        is ``INVALID_MEASUREMENT`` (the timeout was not induced). Recovery is claimed only when the runtime answers
        its health endpoint AND a known-good inference succeeds afterwards. Server-side cancellation of the
        abandoned generation is not observable here and is never claimed."""
        deadline = self.timeout_deadline_s
        entry = self._call("timeout", "total_deadline", (DETERMINISM_PROMPT[0], {
            "role": "user", "content": "Write a 600-word essay about cooling pumps."}), num_ctx=8192, num_predict=900,
            think=False, deadline_s=deadline)
        induced = entry.get("error") == "TOTAL_DEADLINE_EXCEEDED"
        within = induced and float(entry.get("elapsed_s") or 0) <= deadline + 1.0
        try:
            health = bool(self.client.get("/api/version").get("version"))
        except httpx.HTTPError:
            health = False
        after = self._call("timeout", "after_timeout_inference", DETERMINISM_PROMPT, num_ctx=8192, num_predict=32,
                           think=False)
        status = "MEASURED" if induced else "INVALID_MEASUREMENT"
        self.record({"record_type": "check", "phase": "timeout", "task_id": "timeout_recovery", "category": "stability",
                     "think": False, "passed": bool(induced and within and health and after["ok"]),
                     "details": {"measurement": status, "deadline_s": deadline, "connect_timeout_s": CONNECT_TIMEOUT_S,
                                 "timed_out": entry.get("error"), "elapsed_s": entry.get("elapsed_s"),
                                 "first_token_s": entry.get("first_token_s"),
                                 "chunks_before_deadline": entry.get("chunks_before_deadline"),
                                 "deadline_honoured": within, "client_cancellation": entry.get("client_cancellation"),
                                 "server_side_cancellation": "NOT_OBSERVABLE", "post_timeout_health": health,
                                 "post_timeout_inference_ok": after["ok"], "recovered": bool(health and after["ok"])}})

    def reload_cycle(self, cycles: int = 2) -> None:
        """Unload/reload the model (``keep_alive=0``); a runtime PROCESS restart is an operator action, not automated."""
        oks = []
        for index in range(cycles):
            self.client.unload(self.model)
            entry = self._call("reload", f"reload_{index + 1}", DETERMINISM_PROMPT, num_ctx=8192, num_predict=32,
                               think=False)
            oks.append(entry["ok"])
        self.record({"record_type": "check", "phase": "reload", "task_id": "reload_cycles", "category": "stability",
                     "think": False, "passed": all(oks), "details": {"cycles": cycles, "ok": oks,
                                                                     "process_restart": "NOT_AUTOMATED"}})

    def run_all(self) -> None:
        self.record({"record_type": "meta", "benchmark_version": BENCHMARK_VERSION, "model": self.model,
                     "task_set_digest": task_set_digest(quality_tasks() + long_context_tasks()), "seed": SEED,
                     "started_at": datetime.now(UTC).isoformat()})
        for name, phase in (("latency", self.latency), ("quality", self.quality), ("context", self.context),
                            ("determinism", self.determinism), ("concurrency", self.concurrency),
                            ("timeout", self.timeout_behaviour), ("reload", self.reload_cycle)):
            self.log(f"phase {name}")
            try:
                phase()
            except (httpx.HTTPError, BenchmarkRefused) as exc:
                self.record({"record_type": "phase_error", "phase": name, "error": type(exc).__name__})
        self.record({"record_type": "meta_end", "finished_at": datetime.now(UTC).isoformat()})


@dataclass(frozen=True)
class PinnedArtifact:
    """The audited immutable identity. A matching tag name never satisfies it on its own."""

    model: str
    manifest_digest: str
    layers: tuple[str, ...] = ()  # layer digests the local manifest must list; empty means layers are not pinned


def normalize_digest(value: object) -> str | None:
    """``sha256:<64 lower-case hex>`` from the prefixed or bare form a runtime reports; ``None`` for anything else."""
    if not isinstance(value, str):
        return None
    hex_digest = value.strip().lower().removeprefix("sha256:")
    return f"sha256:{hex_digest}" if re.fullmatch(r"[0-9a-f]{64}", hex_digest) else None


def identity_refusal(pin: PinnedArtifact, observed_manifest_digest: object, manifest_bytes: bytes | None) -> str | None:
    """Refusal code when the observed artifact is not the pinned one, else ``None``.

    The manifest digest names the whole artifact and a layer digest names one blob inside it; the two are never
    compared with each other. With pinned layers, the local manifest bytes must hash to the pinned manifest digest
    and list every pinned layer.
    """
    expected = normalize_digest(pin.manifest_digest)
    layers = tuple(normalize_digest(layer) for layer in pin.layers)
    if expected is None or None in layers or expected in layers:
        return "PIN_INVALID"
    if normalize_digest(observed_manifest_digest) != expected:
        return "MANIFEST_DIGEST_MISMATCH"
    if not layers:
        return None
    if manifest_bytes is None:
        return "LOCAL_MANIFEST_UNAVAILABLE"
    if f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}" != expected:
        return "LOCAL_MANIFEST_DIGEST_MISMATCH"
    try:
        listed = {normalize_digest(layer.get("digest")) for layer in json.loads(manifest_bytes)["layers"]}
    except (ValueError, KeyError, TypeError, AttributeError):
        return "LOCAL_MANIFEST_UNPARSEABLE"
    return None if set(layers) <= listed else "LAYER_DIGEST_MISMATCH"


def local_manifest_path(models_dir: Path, model: str) -> Path:
    """Where Ollama keeps a library model's manifest, e.g. ``manifests/registry.ollama.ai/library/qwen3/8b``."""
    name, _, tag = model.partition(":")
    return models_dir / "manifests" / "registry.ollama.ai" / "library" / name / (tag or "latest")


def _redact_urls(message: object) -> object:
    """Drop URL query strings (pre-signed download URLs carry signatures) from runtime error text."""
    return re.sub(r"(https?://[^\s\"?]+)\?[^\s\"]*", r"\1?<redacted>", message) if isinstance(message, str) else message


def verify_present(client: OllamaClient, pin: PinnedArtifact, read_manifest: Callable[[], bytes | None]) -> dict[str, Any]:
    """Identity of the model the runtime lists under the pinned tag; raises unless it is the pinned artifact."""
    tags = client.get("/api/tags").get("models") or []
    entry = next((m for m in tags if isinstance(m, dict) and m.get("name") == pin.model), None)
    observed = normalize_digest(entry.get("digest")) if entry else None
    manifest_bytes = read_manifest() if entry else None
    refusal = "MODEL_ABSENT" if entry is None else identity_refusal(pin, observed, manifest_bytes)
    return {"expected_manifest_digest": normalize_digest(pin.manifest_digest), "observed_manifest_digest": observed,
            "local_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest() if manifest_bytes is not None else None,
            "pinned_layers": list(pin.layers), "size_bytes": entry.get("size") if entry else None,
            "identity_refusal": refusal, "digest_match": refusal is None}


def pull_and_verify(client: OllamaClient, pin: PinnedArtifact, record: Record,
                    read_manifest: Callable[[], bytes | None]) -> dict[str, Any]:
    """Pull through the runtime, then require the pinned identity. A failed pull is ``PULL_FAILED``, never a mismatch."""
    started = time.perf_counter()
    last: dict[str, Any] = {}
    statuses: list[str] = []
    transport_error: str | None = None
    try:
        for chunk in client.stream("/api/pull", {"model": pin.model, "stream": True}, timeout=7200):
            last = chunk
            status = str(chunk.get("status", ""))
            if not statuses or statuses[-1] != status:
                statuses.append(status[:120])
            if chunk.get("error"):
                break
    except httpx.HTTPError as exc:
        transport_error = type(exc).__name__
    duration = time.perf_counter() - started
    pulled = transport_error is None and not last.get("error") and last.get("status") == "success"
    result = {"record_type": "pull", "model": pin.model, "duration_s": round(duration, 1),
              "final_status": last.get("status"), "error": _redact_urls(last.get("error")) or transport_error,
              "pull_outcome": "SUCCESS" if pulled else "FAILED", "status_sequence": statuses[:50],
              **verify_present(client, pin, read_manifest)}
    record(result)
    if not pulled:
        raise BenchmarkRefused("PULL_FAILED")
    if result["identity_refusal"]:
        raise BenchmarkRefused(f"PULLED_{result['identity_refusal']}")
    return result


# --- M4.1 integrity instrumentation -----------------------------------------------------------------------------------

SERVER_CONFIG_KEYS = ("OLLAMA_MODELS", "OLLAMA_CONTEXT_LENGTH", "OLLAMA_KV_CACHE_TYPE", "OLLAMA_FLASH_ATTENTION",
                      "OLLAMA_NUM_PARALLEL", "OLLAMA_MAX_LOADED_MODELS", "OLLAMA_MAX_QUEUE", "OLLAMA_GPU_OVERHEAD",
                      "OLLAMA_KEEP_ALIVE", "OLLAMA_HOST")


def _redact_home(value: str) -> str:
    """Paths under a user profile keep their shape but not the account name."""
    return re.sub(r"(?i)([a-z]:\\users\\|/home/|/Users/)[^\\/]+", r"\1<user>", value)


def _allowlisted(environment: dict[str, str], keys: tuple[str, ...]) -> dict[str, str | None]:
    return {key: (_redact_home(environment[key])[:200] if key in environment else None) for key in keys}


def server_processes(name_fragment: str) -> list[Any]:
    """Runtime SERVER processes: name matches and the parent does not (a child of a server is a worker)."""
    matches = []
    for process in psutil.process_iter(["pid", "ppid", "name"]):
        name = (process.info.get("name") or "").lower()
        if name_fragment in name:
            matches.append(process)
    pids = {p.info["pid"] for p in matches}
    return [p for p in matches if p.info.get("ppid") not in pids]


def server_config_snapshot(name_fragment: str, keys: tuple[str, ...] = SERVER_CONFIG_KEYS) -> dict[str, Any]:
    """SERVER_EFFECTIVE_CONFIGURATION is read from the server process's own environment (allowlisted keys only);
    CLIENT_PROCESS_ENVIRONMENT is this process's. Neither is inferred from the other: unreadable stays UNKNOWN."""
    servers = server_processes(name_fragment)
    server: dict[str, Any] = {"status": "UNKNOWN", "reason": "SERVER_PROCESS_NOT_FOUND", "values": None}
    if len(servers) > 1:
        server = {"status": "UNKNOWN", "reason": "MULTIPLE_SERVER_PROCESSES", "values": None,
                  "server_pids": sorted(p.info["pid"] for p in servers)}
    elif servers:
        try:
            environment = servers[0].environ()
            server = {"status": "CAPTURED", "reason": None, "server_pid": servers[0].info["pid"],
                      "values": _allowlisted(environment, keys),
                      "unset_means": "runtime default (not visible in the environment)"}
        except (psutil.AccessDenied, psutil.NoSuchProcess, OSError) as exc:
            server = {"status": "UNKNOWN", "reason": f"ENVIRON_UNREADABLE:{type(exc).__name__}", "values": None,
                      "server_pid": servers[0].info["pid"]}
    import os

    return {"schema": "pados.local-intelligence.server-config.v1", "SERVER_EFFECTIVE_CONFIGURATION": server,
            "CLIENT_PROCESS_ENVIRONMENT": _allowlisted(dict(os.environ), keys)}


def gpu_compute_apps() -> dict[int, int | None] | None:
    """pid -> used GPU memory (MiB, None when the driver reports N/A, e.g. under WDDM); None when unavailable."""
    try:
        result = run_command(("nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"), 5)
    except (CommandNotFound, CommandTimedOut):
        return None
    if result.returncode != 0:
        return None
    apps: dict[int, int | None] = {}
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) == 2 and parts[0].isdigit():
            apps[int(parts[0])] = int(parts[1]) if parts[1].isdigit() else None
    return apps


def process_tree_snapshot(name_fragment: str) -> dict[str, Any]:
    """Server PID, its descendant worker PIDs, their memory and GPU attribution. Attribution is claimed only when
    a descendant of the server is observed with readable memory or appears in the GPU compute-app list."""
    servers = server_processes(name_fragment)
    apps = gpu_compute_apps()
    tree: list[dict[str, Any]] = []

    def describe(process: Any, role: str, parent: int | None) -> dict[str, Any]:
        entry: dict[str, Any] = {"pid": process.pid, "ppid": parent, "role": role, "name": process.name()[:60]}
        try:
            memory = process.memory_info()
            entry.update(rss_mib=memory.rss // 2**20, vms_mib=memory.vms // 2**20,
                         private_mib=(getattr(memory, "private", 0) or 0) // 2**20 or None)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            entry["memory"] = "UNREADABLE"
        if apps is not None:
            entry["gpu_listed"] = process.pid in apps
            entry["gpu_used_mib"] = apps.get(process.pid)
        return entry

    for server in servers:
        try:
            tree.append(describe(server, "SERVER", server.info.get("ppid")))
            for child in server.children(recursive=True):
                tree.append(describe(child, "WORKER", child.ppid()))
        except psutil.NoSuchProcess:
            continue
    workers = [item for item in tree if item["role"] == "WORKER"]
    attributed = any(("rss_mib" in w) or w.get("gpu_listed") for w in workers)
    return {"schema": "pados.local-intelligence.process-tree.v1", "processes": tree,
            "gpu_compute_apps_available": apps is not None,
            "PROCESS_MEMORY_ATTRIBUTION": "ATTRIBUTED_TO_WORKER" if attributed and len(servers) == 1 else "UNKNOWN",
            "basis": "process tree (psutil) + nvidia-smi compute apps"}


def concurrency_semantics(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Separate acceptance, completion, queueing and overlap from per-request monotonic stamps.

    REQUESTS_COMPLETED is never read as parallel inference: generation is PARALLEL only when two requests'
    generation intervals [first token, end] overlap."""
    ok = [r for r in results if r.get("ok")]
    timed = [r for r in ok if all(isinstance(r.get(k), (int, float)) for k in ("t_start", "t_first", "t_end"))]
    overlaps = 0
    overlap_s = 0.0
    queued = 0
    for index, a in enumerate(timed):
        for b in timed[index + 1:]:
            span = min(a["t_end"], b["t_end"]) - max(a["t_first"], b["t_first"])
            if span > 0:
                overlaps += 1
                overlap_s = max(overlap_s, span)
        if any(other is not a and other["t_end"] <= a["t_first"] and other["t_start"] <= a["t_start"] + 0.05
               for other in timed):
            queued += 1
    return {"requests_submitted": len(results), "requests_accepted": len(ok), "requests_completed": len(ok),
            "requests_timed": len(timed), "requests_queued_behind_another": queued,
            "overlapping_generation_pairs": overlaps, "max_generation_overlap_s": round(overlap_s, 4),
            "parallel_token_generation": ("UNKNOWN" if len(timed) < 2 else "OBSERVED" if overlaps
                                          else "NOT_OBSERVED_SERIALIZED")}
