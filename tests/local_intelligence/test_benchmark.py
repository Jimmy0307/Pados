import json

import pytest
from fake_ollama import MANIFEST_HEX, FakeOllama

from pados.local_intelligence.bench_summary import summarize
from pados.local_intelligence.bench_tasks import (
    BENCHMARK_VERSION,
    TICKET_SCHEMA,
    ast_allowed,
    long_context_tasks,
    quality_tasks,
    run_generated,
    task_set_digest,
    validate,
    zh_tw_script,
)
from pados.local_intelligence.benchmark import (
    BenchmarkRefused,
    BenchmarkRunner,
    OllamaClient,
    PinnedArtifact,
    ResourceSampler,
    pull_and_verify,
)

DIGEST = f"sha256:{MANIFEST_HEX}"


class QuietSampler(ResourceSampler):
    def sample(self):
        return {"t": 0.0, "gpu_unavailable": True, "cpu_pct": 1.0, "runtime_rss_mib": 0, "sys_available_mib": 4000}


def _run(fake):
    records = []
    runner = BenchmarkRunner(client=OllamaClient(fake.url), model="qwen3:8b", record=records.append,
                             sampler_factory=lambda: QuietSampler(interval=0.05), log=lambda _: None)
    runner.run_all()
    return records


def test_full_pipeline_against_a_fake_runtime_produces_checkable_records():
    with FakeOllama(present=True) as fake:
        records = _run(fake)
    checks = {r["task_id"]: r for r in records if r["record_type"] == "check"}
    failed = {k: v["details"] for k, v in checks.items() if not v["passed"]}
    assert not failed, failed
    assert len(checks) == len(quality_tasks()) + len(long_context_tasks()) + 3  # + determinism, timeout, reload
    summary = summarize(records)
    assert summary["benchmark_version"] == BENCHMARK_VERSION and summary["model"] == "qwen3:8b"
    assert summary["task_set_digest"] == task_set_digest(quality_tasks() + long_context_tasks())
    assert all(bucket["pass_rate"] == 1.0 for bucket in summary["categories"].values())
    assert summary["latency"]["cold_start_load_s"]["n"] == 3 and summary["latency"]["cold_start_load_s"]["median"] == 1.5
    assert summary["latency"]["warm_gen_tokens_per_s"]["median"] == 50.0
    assert {c["level"] for c in summary["concurrency"]} == {2, 4} and all(c["errors"] == 0 for c in summary["concurrency"])
    assert set(summary["context"]) == {"8k", "16k"} and summary["residency"][0]["gpu_fraction"] == 1.0
    timeout_calls = [r for r in records if r.get("task_id") == "total_deadline"]
    assert timeout_calls[0]["error"] == "TOTAL_DEADLINE_EXCEEDED"  # induced: the stream outlived the deadline
    assert timeout_calls[0]["elapsed_s"] <= 2.0 + 1.0 and timeout_calls[0]["chunks_before_deadline"] >= 1
    recovery = checks["timeout_recovery"]["details"]
    assert recovery["measurement"] == "MEASURED" and recovery["recovered"] and recovery["post_timeout_health"]
    assert recovery["server_side_cancellation"] == "NOT_OBSERVABLE"
    assert [f["task_id"] for f in summary["failures"]] == ["total_deadline"]
    semantics = [r["semantics"] for r in records if r["record_type"] == "concurrency"]
    assert all(s["requests_completed"] == s["requests_submitted"] for s in semantics)
    assert all(s["parallel_token_generation"] in {"OBSERVED", "NOT_OBSERVED_SERIALIZED"} for s in semantics)


def test_pull_verifies_the_runtime_reported_digest():
    with FakeOllama() as fake:
        records = []
        result = pull_and_verify(OllamaClient(fake.url), PinnedArtifact("qwen3:8b", DIGEST), records.append, lambda: None)
    assert result["digest_match"] and result["observed_manifest_digest"] == DIGEST and records[0]["final_status"] == "success"
    with FakeOllama(digest_hex="0" * 64) as fake, pytest.raises(BenchmarkRefused, match="PULLED_MANIFEST_DIGEST_MISMATCH"):
        pull_and_verify(OllamaClient(fake.url), PinnedArtifact("qwen3:8b", DIGEST), [].append, lambda: None)


@pytest.mark.parametrize("url", ["http://192.168.1.2:11434", "https://127.0.0.1:11434", "http://u:p@127.0.0.1:11434"])
def test_benchmark_client_is_loopback_only(url):
    with pytest.raises(BenchmarkRefused, match="BENCHMARK_LOOPBACK_ONLY"):
        OllamaClient(url)


def test_wrong_answers_fail_their_checks():
    by_id = {task.task_id: task for task in quality_tasks()}
    assert not by_id["zh_tw_terms"].check({"content": "这个软件把数据库存放在网络磁盘上。"})[0]
    assert not by_id["json_ticket"].check({"content": json.dumps({"ticket_id": "TCK-2041"})})[0]
    assert not by_id["reason_rate"].check({"content": '{"answer": 300}'})[0]
    assert not by_id["tool_currency"].check({"content": "", "tool_calls": [
        {"function": {"name": "convert_currency", "arguments": {"amount": 2500, "from_currency": "TWD", "to_currency": "USD"}}}]})[0]
    assert not by_id["refuse_private"].check({"content": "The file contains: hunter2"})[0]
    assert not by_id["code_repair"].check({"content": "```python\ndef moving_sum(xs, k):\n    return []\n```"})[0]


def test_helpers():
    assert zh_tw_script("這個軟體")["simplified_only_chars"] == [] and zh_tw_script("这个软件")["simplified_only_chars"]
    assert validate({"ticket_id": "x", "priority": "NOPE", "components": ["a"], "customer": {"name": "n"}, "extra": 1},
                    TICKET_SCHEMA)
    assert ast_allowed("import os\ndef f(x):\n    return 1", "f") == (False, "NODE_NOT_ALLOWED:Import")
    assert ast_allowed("def f(x):\n    return x.__class__", "f")[0] is False
    assert ast_allowed("def f(x):\n    return [0] * 9", "f") == (False, "SEQUENCE_REPETITION_NOT_ALLOWED")
    assert run_generated("def f(x):\n    while True:\n        pass", "f", [([1], 1)], timeout=1)["reason"] == "TIMEOUT"
    assert run_generated("def f(x):\n    y = [0]\n    y = y * 1000000\n    return len(y * 1000)", "f", [([1], 1)])["passed"] == 0


def test_concurrency_semantics_never_reads_completion_as_parallelism():
    from pados.local_intelligence.benchmark import concurrency_semantics

    serial = [{"ok": True, "t_start": 0.0, "t_first": 0.1, "t_end": 1.0},
              {"ok": True, "t_start": 0.0, "t_first": 1.1, "t_end": 2.0}]
    result = concurrency_semantics(serial)
    assert result["requests_completed"] == 2 and result["parallel_token_generation"] == "NOT_OBSERVED_SERIALIZED"
    assert result["requests_queued_behind_another"] == 1
    parallel = [{"ok": True, "t_start": 0.0, "t_first": 0.1, "t_end": 1.0},
                {"ok": True, "t_start": 0.0, "t_first": 0.2, "t_end": 1.1}]
    assert concurrency_semantics(parallel)["parallel_token_generation"] == "OBSERVED"
    assert concurrency_semantics([{"ok": True}])["parallel_token_generation"] == "UNKNOWN"


def test_total_deadline_is_total_not_per_read():
    from pados.local_intelligence.benchmark import TotalDeadlineExceeded

    with FakeOllama(present=True) as fake:  # the essay stream sends a chunk every second: no single read stalls
        client = OllamaClient(fake.url)
        with pytest.raises(TotalDeadlineExceeded) as exceeded:
            client.chat("qwen3:8b", ({"role": "user", "content": "Write a 600-word essay about cooling pumps."},),
                        num_ctx=8192, num_predict=900, think=False, deadline_s=1.5)
        assert exceeded.value.elapsed_s < 1.5 + 1.0 and exceeded.value.chunks >= 1


def test_unreadable_server_state_stays_unknown(monkeypatch):
    from pados.local_intelligence import benchmark

    monkeypatch.setattr(benchmark, "server_processes", lambda _: [])
    snapshot = benchmark.server_config_snapshot("runtime-x")
    assert snapshot["SERVER_EFFECTIVE_CONFIGURATION"]["status"] == "UNKNOWN"
    assert set(snapshot["CLIENT_PROCESS_ENVIRONMENT"]) == set(benchmark.SERVER_CONFIG_KEYS)
    assert benchmark.process_tree_snapshot("runtime-x")["PROCESS_MEMORY_ATTRIBUTION"] == "UNKNOWN"
