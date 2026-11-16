# SPDX-License-Identifier: AGPL-3.0-only
"""Resources (資源與儲存) collectors: every metric carries source, unit, time and measured/UNKNOWN; anything not measured is
UNKNOWN with a closed reason and no value; the model runtime is never called unless an Ollama provider is configured on
a loopback endpoint. The database is replaced by a scripted stand-in (no PostgreSQL needed)."""
from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from pados.control import resource_metrics as rm
from pados.control import resource_summary
from pados.local_intelligence.runtimes import FetchError

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


class Result:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one(self) -> Any:
        return self.value

    def scalar(self) -> Any:
        return self.value

    def mappings(self) -> Result:
        return self

    def all(self) -> Any:
        return self.value

    def one(self) -> Any:
        return self.value


class ScriptedSession:
    """Answers by the first matching SQL fragment; raises for a fragment mapped to an exception."""

    def __init__(self, answers: list[tuple[str, Any]]) -> None:
        self.answers = answers
        self.statements: list[str] = []

    def execute(self, statement: Any, _params: Any = None) -> Result:
        sql = str(statement)
        self.statements.append(sql)
        for fragment, answer in self.answers:
            if fragment in sql:
                if isinstance(answer, Exception):
                    raise answer
                return Result(answer)
        raise AssertionError(f"unexpected SQL: {sql[:120]}")


DB_DOWN = OperationalError("SELECT 1", {}, Exception("connection refused"))


def _assert_unknown(metric: rm.Metric, reason_prefix: str) -> None:
    body = metric.to_dict()
    assert body["status"] == "UNKNOWN" and body["measured"] is False
    assert body["value"] is None and body["measured_at"] is None
    assert str(body["reason"]).startswith(reason_prefix)
    assert body["source"] and body["unit"]


def _assert_measured(metric: rm.Metric) -> dict[str, Any]:
    body = metric.to_dict()
    assert body["status"] == "MEASURED" and body["measured"] is True and body["reason"] is None
    assert body["measured_at"] == NOW.isoformat() and body["source"] and body["unit"]
    return body


# --- RAM --------------------------------------------------------------------------------------------------------------
def test_ram_is_measured_from_the_probes() -> None:
    ram, rss = rm.ram_metrics(NOW, host=lambda: (6, 16, 10), process=lambda: 3)
    assert _assert_measured(ram)["value"] == 6 and ram.detail["total_bytes"] == 16 and ram.detail["used_fraction"] == 0.375
    assert _assert_measured(rss)["value"] == 3


def test_ram_probe_failure_is_unknown() -> None:
    def broken() -> tuple[int, int, int]:
        raise OSError("no /proc")

    def broken_rss() -> int:
        raise RuntimeError("denied")

    ram, rss = rm.ram_metrics(NOW, host=broken, process=broken_rss)
    _assert_unknown(ram, "RAM_PROBE_FAILED:OSError")
    _assert_unknown(rss, "PROCESS_PROBE_FAILED:RuntimeError")


def test_the_default_ram_probe_reads_this_host() -> None:
    ram, rss = rm.ram_metrics(NOW)
    assert ram.measured and isinstance(ram.value, int) and ram.value > 0
    assert rss.measured and isinstance(rss.value, int) and rss.value > 0


# --- model resident memory --------------------------------------------------------------------------------------------
def _never(_host: str, _port: int) -> Callable[[str], bytes]:
    raise AssertionError("the model runtime must not be called")


def _settings(**values: Any) -> SimpleNamespace:
    base = {"chat_provider": "", "fast_provider": "", "executor_provider": "", "embed_provider": "mock",
            "ollama_base_url": "http://127.0.0.1:11434"}
    return SimpleNamespace(**{**base, **values})


def test_model_memory_is_unknown_and_not_called_without_a_configured_provider() -> None:
    _assert_unknown(rm.model_memory_metric(_settings(), NOW, fetch_factory=_never), "MODEL_RUNTIME_NOT_CONFIGURED")


def test_model_memory_never_calls_a_non_loopback_endpoint() -> None:
    metric = rm.model_memory_metric(_settings(chat_provider="ollama", ollama_base_url="http://203.0.113.5:11434"), NOW,
                                    fetch_factory=_never)
    _assert_unknown(metric, "MODEL_RUNTIME_NOT_LOOPBACK")


def test_model_memory_sums_what_the_configured_runtime_reports() -> None:
    calls: list[tuple[str, int, str]] = []

    def factory(host: str, port: int) -> Callable[[str], bytes]:
        def fetch(path: str) -> bytes:
            calls.append((host, port, path))
            return json.dumps({"models": [
                {"name": "qwen3:8b", "size": 6_000, "size_vram": 5_000, "context_length": 4096},
                {"name": "nomic-embed-text:latest", "size": 1_000, "size_vram": 0},
                {"name": "../bad", "size": 9}]}).encode()
        return fetch

    metric = rm.model_memory_metric(_settings(executor_provider="Ollama"), NOW, fetch_factory=factory)
    body = _assert_measured(metric)
    assert body["value"] == 7_000 and body["detail"]["vram_bytes"] == 5_000 and body["detail"]["loaded_models"] == 2
    assert calls == [("127.0.0.1", 11434, "/api/ps")]
    assert body["detail"]["providers"] == ["executor_provider"]


@pytest.mark.parametrize(("answer", "reason"), [
    (FetchError("CONNECTION_REFUSED"), "MODEL_RUNTIME_CONNECTION_REFUSED"),
    (b"not json", "MODEL_RUNTIME_RESPONSE_INVALID"),
    (b'{"models": "x"}', "MODEL_RUNTIME_RESPONSE_INVALID"),
    (b'{"models": [{"name": "m:1"}]}', "MODEL_SIZE_NOT_REPORTED"),
])
def test_model_memory_failures_are_unknown(answer: Any, reason: str) -> None:
    def factory(_host: str, _port: int) -> Callable[[str], bytes]:
        def fetch(_path: str) -> bytes:
            if isinstance(answer, Exception):
                raise answer
            return bytes(answer)
        return fetch

    _assert_unknown(rm.model_memory_metric(_settings(chat_provider="ollama"), NOW, fetch_factory=factory), reason)


def test_an_empty_runtime_reports_zero_resident_bytes() -> None:
    metric = rm.model_memory_metric(_settings(chat_provider="ollama"), NOW,
                                    fetch_factory=lambda _h, _p: lambda _path: b'{"models": []}')
    assert _assert_measured(metric)["value"] == 0


# --- PostgreSQL -------------------------------------------------------------------------------------------------------
def test_database_sizes_and_indexes_are_measured() -> None:
    session = ScriptedSession([
        ("current_database()", "pados_ops"),
        ("FROM pg_database", [{"datname": "pados_corpus", "bytes": 100}, {"datname": "pados_ops", "bytes": 900}]),
        ("pg_indexes_size", 40),
    ])
    size, indexes = rm.database_metrics(session, SimpleNamespace(db_name="pados_ops", corpus_database_name="pados_corpus"),
                                        NOW)
    body = _assert_measured(size)
    assert body["value"] == 1000
    assert body["detail"]["databases"] == [{"name": "pados_corpus", "bytes": 100, "current": False},
                                           {"name": "pados_ops", "bytes": 900, "current": True}]
    assert _assert_measured(indexes)["value"] == 40


def test_database_unavailable_is_unknown() -> None:
    size, indexes = rm.database_metrics(ScriptedSession([("current_database()", DB_DOWN)]), SimpleNamespace(), NOW)
    _assert_unknown(size, "DB_NOT_AVAILABLE")
    _assert_unknown(indexes, "DB_NOT_AVAILABLE")


# --- execution growth, tokens and execution time ----------------------------------------------------------------------
def test_daily_series_is_zero_filled_oldest_first() -> None:
    series = rm.daily_series([{"day": "2026-11-16", "count": 3}, {"day": "2026-11-10", "count": 1}], NOW, days=7)
    assert [p["day"] for p in series] == [f"2026-11-{d}" for d in range(10, 17)]
    assert [p["count"] for p in series] == [1, 0, 0, 0, 0, 0, 3]


def test_execution_growth_comes_from_row_timestamps() -> None:
    session = ScriptedSession([
        ("to_regclass", "present"),
        ("SELECT count(*) FROM", 5),
        ("GROUP BY 1", [{"day": "2026-11-16", "count": 2}]),
    ])
    body = _assert_measured(rm.execution_growth_metric(session, NOW))
    assert body["value"] == 5 * len(rm.EXECUTION_TABLES)
    assert body["detail"]["series"][-1] == {"day": "2026-11-16", "count": 2 * len(rm.EXECUTION_TABLES)}
    assert len(body["detail"]["series"]) == rm.TREND_DAYS
    assert all(table["status"] == "MEASURED" for table in body["detail"]["tables"])


def test_execution_growth_unknown_when_the_database_fails() -> None:
    _assert_unknown(rm.execution_growth_metric(ScriptedSession([("to_regclass", DB_DOWN)]), NOW), "DB_NOT_AVAILABLE")


def _usage(measured_jobs: int, timed_jobs: int, wall_jobs: int) -> ScriptedSession:
    return ScriptedSession([
        ("measured_execution", {"jobs": 3, "measured_jobs": measured_jobs, "prompt_tokens": 1200,
                                "output_tokens": 300, "timed_jobs": timed_jobs, "model_ns": 2_500_000_000}),
        ("EXTRACT(EPOCH", {"jobs": wall_jobs, "seconds": 9.0}),
    ])


def test_token_usage_and_execution_time_are_measured_from_recorded_evidence() -> None:
    tokens, model_time, job_time = rm.usage_metrics(_usage(2, 2, 3), NOW)
    assert _assert_measured(tokens)["value"] == 1500 and tokens.detail["prompt_tokens"] == 1200
    assert _assert_measured(model_time)["value"] == 2.5
    assert _assert_measured(job_time)["value"] == 9.0 and job_time.detail["average_seconds"] == 3.0


def test_no_measured_model_execution_is_unknown_not_zero() -> None:
    tokens, model_time, job_time = rm.usage_metrics(_usage(0, 0, 0), NOW)
    _assert_unknown(tokens, "NO_MEASURED_MODEL_EXECUTION")
    _assert_unknown(model_time, "NO_MEASURED_MODEL_EXECUTION")
    _assert_unknown(job_time, "NO_FINISHED_JOBS_IN_WINDOW")


def test_usage_unknown_when_the_database_fails() -> None:
    for metric in rm.usage_metrics(ScriptedSession([("measured_execution", DB_DOWN)]), NOW):
        _assert_unknown(metric, "DB_NOT_AVAILABLE")


# --- the assembled summary --------------------------------------------------------------------------------------------
def _app_settings(tmp_path: Path, **values: Any) -> SimpleNamespace:
    runtime = tmp_path / "runtime"
    base = {"artifact_root": str(runtime / "artifacts"), "product_workspace_root": "", "product_object_root": "",
            "remote_config": "", "compute_nodes_config": "", "connectivity_config": "", "storage_policy_path": "",
            "db_name": "pados_ops", "operational_database_name": "", "corpus_database_name": "",
            "product_database_name": "", "chat_provider": "", "fast_provider": "", "executor_provider": "",
            "embed_provider": "mock", "ollama_base_url": "http://127.0.0.1:11434"}
    return SimpleNamespace(**{**base, **values})


@contextlib.contextmanager
def _down(_settings: object = None) -> Iterator[Any]:
    raise DB_DOWN
    yield  # pragma: no cover


def test_summary_without_a_database_marks_every_database_metric_unknown(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "demo").mkdir(parents=True)
    (workspace / "demo" / "guide.md").write_bytes(b"# guide\n")
    (tmp_path / "runtime" / "cache").mkdir(parents=True)
    (tmp_path / "runtime" / "cache" / "x.bin").write_bytes(b"0" * 10)
    summary = resource_summary.resources_summary(
        _app_settings(tmp_path, product_workspace_root=str(workspace)), _down, now=NOW, fetch_factory=_never,
        disk_probe=lambda _p: (1000, 500), host_memory=lambda: (1, 2, 1), process_memory=lambda: 1)
    metrics = {m["id"]: m for m in summary["metrics"]}
    assert summary["authority"] == "PROJECTION_ONLY"
    for metric_id in ("database_size", "database_index_size", "execution_records", "token_usage",
                      "model_execution_time", "job_execution_time"):
        assert metrics[metric_id]["status"] == "UNKNOWN" and metrics[metric_id]["value"] is None, metric_id
        assert metrics[metric_id]["reason"].startswith("DB_NOT_AVAILABLE")
    assert metrics["model_resident_memory"]["reason"] == "MODEL_RUNTIME_NOT_CONFIGURED"
    assert metrics["project_documents"]["status"] == "MEASURED" and metrics["project_documents"]["value"] == 8
    assert metrics["cache_size"]["value"] == 10
    assert metrics["evidence_files"]["status"] == "UNKNOWN"  # nothing exists at the artifact root yet
    assert metrics["reclaimable_estimate"]["estimate"] is True
    for metric in summary["metrics"]:
        assert set(metric) >= {"source", "unit", "measured_at", "measured", "status"}
    budget = summary["budget"]
    assert budget["policy_status"] == "DEFAULTS" and budget["budget_configured"] is False
    assert budget["local_bytes"] is None  # the database size is unknown, so the total is too
    assert budget["warning_at_used_fraction"] == 0.85 and budget["critical_at_used_fraction"] == 0.95
    assert budget["disk"]["free_bytes"] == 500 and budget["pressure"] == "NORMAL"
    assert summary["trends"]["storage_bytes"] == {
        "status": "UNKNOWN", "reason": "HISTORY_NOT_RECORDED",
        "detail": summary["trends"]["storage_bytes"]["detail"]}
    assert summary["trends"]["execution_records"]["status"] == "UNKNOWN"
    assert str(tmp_path) not in json.dumps(summary)  # no physical path leaves the summary


def test_a_configured_budget_drives_the_warning_level(tmp_path: Path) -> None:
    policy = tmp_path / "storage-policy.json"
    policy.write_text(json.dumps({"schema_version": "pados.lifecycle.storage-policy.v1",
                                  "budget": {"local_budget_bytes": 1000}}), encoding="utf-8")
    session = ScriptedSession([
        ("current_database()", "pados_ops"), ("FROM pg_database", [{"datname": "pados_ops", "bytes": 900}]),
        ("pg_indexes_size", 0), ("to_regclass", None), ("measured_execution", {
            "jobs": 0, "measured_jobs": 0, "prompt_tokens": 0, "output_tokens": 0, "timed_jobs": 0, "model_ns": 0}),
        ("EXTRACT(EPOCH", {"jobs": 0, "seconds": 0}),
    ])

    @contextlib.contextmanager
    def scope(_settings: object = None) -> Iterator[ScriptedSession]:
        yield session

    summary = resource_summary.resources_summary(
        _app_settings(tmp_path, storage_policy_path=str(policy)), scope, now=NOW, fetch_factory=_never,
        disk_probe=lambda _p: (10_000, 9_000), host_memory=lambda: (1, 2, 1), process_memory=lambda: 1)
    budget = summary["budget"]
    assert budget["policy_status"] == "CONFIGURED" and budget["local_budget_bytes"] == 1000
    assert budget["local_bytes"] == 900 and budget["used_fraction"] == 0.9 and budget["pressure"] == "PRESSURED"
    assert summary["trends"]["execution_records"]["status"] == "MEASURED"


def test_an_invalid_policy_falls_back_to_defaults_and_says_so(tmp_path: Path) -> None:
    policy = tmp_path / "storage-policy.json"
    policy.write_text("{\"schema_version\": \"other\"}", encoding="utf-8")
    loaded, status = resource_summary.load_policy(SimpleNamespace(storage_policy_path=str(policy)))
    assert status.startswith("POLICY_INVALID") and loaded.budget.local_budget_bytes is None
