import json
import time
import uuid

import pytest
from node_support import auth, make_node

from pados.compute_node.backends import (
    BackendError,
    OllamaBackend,
    OpenAICompatibleBackend,
    mlx_openai_compatible,
)
from pados.compute_node.manifest import NodeCapability


def _execute(node, **updates):
    body = {"request_id": f"r-{uuid.uuid4()}", "work_package_id": "wc-1", "run_id": "run-1",
            "capability": "LOCAL_INFERENCE", "model_id": "deterministic/pados-deterministic-v1@1",
            "classification": "INTERNAL", "inputs": [{"text": "coolant pump inspection"}], "output_path": "out.md"}
    body.update(updates)
    return node.client.post("/execute", json=body, headers=auth())


def _wait(node, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        record = node.client.get(f"/jobs/{job_id}", headers=auth()).json()
        if record["state"] not in {"QUEUED", "RUNNING"} or time.monotonic() > deadline:
            return record
        time.sleep(0.02)


def test_every_endpoint_requires_the_node_credential(node):
    for method, path in [("GET", "/health"), ("GET", "/capabilities"), ("GET", "/models"), ("POST", "/execute"),
                         ("GET", "/jobs/x"), ("POST", "/jobs/x/cancel"), ("GET", "/artifacts/sha256:" + "0" * 64)]:
        assert node.client.request(method, path).status_code == 401
        assert node.client.request(method, path, headers={"authorization": "Bearer wrong"}).status_code == 401


def test_health_capabilities_and_models(node):
    health = node.client.get("/health", params={"nonce": "n1"}, headers=auth()).json()
    assert health["state"] == "ONLINE" and health["node_id"] == node.node_id and len(health["identity_proof"]) == 64
    caps = node.client.get("/capabilities", headers=auth()).json()
    assert caps["descriptor"]["capabilities"] == ["LOCAL_INFERENCE", "EMBEDDING", "CLASSIFICATION"]
    models = node.client.get("/models", headers=auth()).json()["models"]
    assert models[0]["availability"] == "AVAILABLE"


def test_job_lifecycle_produces_immutable_hashed_artifact_without_content_in_record(node):
    accepted = _execute(node)
    assert accepted.status_code == 202 and accepted.json()["state"] in {"QUEUED", "RUNNING", "SUCCEEDED"}
    record = _wait(node, accepted.json()["job_id"])
    assert record["state"] == "SUCCEEDED" and len(record["artifacts"]) == 1
    artifact = record["artifacts"][0]
    payload = node.client.get(f"/artifacts/{artifact['artifact_id']}", headers=auth())
    assert payload.headers["X-PADOS-Artifact-SHA256"] == artifact["sha256"]
    assert "coolant pump inspection" not in json.dumps(record)  # record carries hashes, not prompts
    assert record["input_sha256"] and record["record_mac"]
    stored = node.runtime.store.root / artifact["sha256"]
    assert stored.stat().st_mode & 0o222 == 0  # read-only
    again = _execute(node, request_id=record["request_id"])
    assert again.json()["job_id"] == record["job_id"]  # idempotent per request id


def test_resource_bounds_fail_closed(node):
    assert _execute(node, inputs=[{"text": "x" * 30_000}]).json()["detail"] == "INPUT_BUDGET_EXCEEDED"
    assert _execute(node, inputs=[{"text": "a"}] * 9).json()["detail"] == "BATCH_TOO_LARGE"
    assert _execute(node, timeout_seconds=31).json()["detail"] == "TIMEOUT_ABOVE_NODE_LIMIT"
    assert _execute(node, model_id="deterministic/missing@1").status_code == 404
    assert _execute(node, capability="OCR").json()["detail"] == "CAPABILITY_NOT_ENABLED"
    slow = [{"text": "slow", "simulate_latency_ms": 800}]
    first = _execute(node, inputs=slow).json()
    second = _execute(node, inputs=slow).json()  # queued (depth 1)
    third = _execute(node, inputs=slow)
    assert third.status_code == 429 and third.json()["detail"] == "QUEUE_FULL"
    assert node.client.get("/health", headers=auth()).json()["state"] == "BUSY"
    for job in (first, second):
        node.client.post(f"/jobs/{job['job_id']}/cancel", headers=auth())


def test_timeout_and_cancellation(tmp_path):
    node = make_node(tmp_path, limits={"max_concurrent_jobs": 2, "max_queue_depth": 2, "default_timeout_seconds": 0.2,
                                       "max_timeout_seconds": 5})
    timed = _wait(node, _execute(node, inputs=[{"text": "t", "simulate_latency_ms": 600}]).json()["job_id"])
    assert timed["state"] == "TIMED_OUT" and timed["error_code"] == "JOB_TIMEOUT" and timed["artifacts"] == []
    running = _execute(node, inputs=[{"text": "c", "simulate_latency_ms": 600}], timeout_seconds=5).json()
    cancelled = node.client.post(f"/jobs/{running['job_id']}/cancel", headers=auth()).json()
    assert cancelled["state"] == "CANCELLED"
    time.sleep(0.8)
    assert _wait(node, running["job_id"])["state"] == "CANCELLED"  # a late result is discarded
    node.runtime.jobs.shutdown()


def test_classification_above_node_ceiling_is_rejected(tmp_path):
    node = make_node(tmp_path, privacy_class="INTERNAL")
    assert _execute(node, classification="RESTRICTED").json()["detail"] == "CLASSIFICATION_ABOVE_NODE_CEILING"
    node.runtime.jobs.shutdown()


def test_batch_and_deterministic_capabilities(node):
    record = _wait(node, _execute(node, capability="CLASSIFICATION", output_path="labels.jsonl", inputs=[
        {"text": "pump leak", "labels": {"maintenance": ["pump", "leak"], "finance": ["price"]}},
        {"text": "price review", "labels": {"maintenance": ["pump"], "finance": ["price"]}}]).json()["job_id"])
    payload = node.client.get(f"/artifacts/{record['artifacts'][0]['artifact_id']}", headers=auth()).text
    lines = [json.loads(line) for line in payload.splitlines()]
    assert [json.loads(line["output"])["label"] for line in lines] == ["maintenance", "finance"]
    embedded = _wait(node, _execute(node, capability="EMBEDDING", output_path="e.json", inputs=[{"text": "pump"}]).json()["job_id"])
    assert embedded["state"] == "SUCCEEDED"


class FakeTransport:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, body, timeout):
        self.calls.append((method, url, body, timeout))
        handler = self.routes.get((method, url.split("//", 1)[1].split("/", 1)[1]))
        if handler is None:
            return 404, {"error": "not found"}
        return handler(body) if callable(handler) else handler


def test_openai_compatible_backend_discovery_chat_and_errors():
    transport = FakeTransport({
        ("GET", "v1/models"): (200, {"data": [{"id": "local-chat"}]}),
        ("POST", "v1/chat/completions"): lambda body: (404, {"error": {"message": "model not found"}})
        if body["model"] != "local-chat" else (200, {"choices": [{"message": {"content": "ok"}}],
                                                    "usage": {"prompt_tokens": 3, "completion_tokens": 1}}),
    })
    backend = OpenAICompatibleBackend("vllm", "http://127.0.0.1:8000", transport=transport)
    assert backend.health() and [m.backend_model for m in backend.discover()] == ["local-chat"]
    out = backend.run(NodeCapability.LOCAL_INFERENCE, "local-chat", {"text": "hi"}, max_output_tokens=8, timeout_seconds=3)
    assert out.content == "ok" and out.runtime["usage"] == {"prompt_tokens": 3, "completion_tokens": 1}
    assert transport.calls[-1][2]["temperature"] == 0 and transport.calls[-1][3] == 3
    with pytest.raises(BackendError, match="MODEL_NOT_FOUND"):
        backend.run(NodeCapability.LOCAL_INFERENCE, "other", {"text": "hi"}, max_output_tokens=8, timeout_seconds=3)
    assert mlx_openai_compatible("mlx", "http://127.0.0.1:8080", transport=transport).kind == "mlx_openai_compatible"

    def down(*_args):
        raise BackendError("ENDPOINT_FAILURE:ConnectError")

    assert OpenAICompatibleBackend("x", "http://127.0.0.1:1", transport=down).health() is False


def test_ollama_adapter_health_models_chat_timeout_and_missing_model():
    transport = FakeTransport({
        ("GET", "api/version"): (200, {"version": "0.0.0"}),
        ("GET", "api/tags"): (200, {"models": [{"name": "local-model:q4", "digest": "abcdef1234567890",
                                                 "details": {"family": "llama", "quantization_level": "Q4_K_M"}}]}),
        ("POST", "api/chat"): lambda body: (404, {"error": f"model '{body['model']}' not found"})
        if body["model"] != "local-model:q4" else (200, {"message": {"content": "hello"}, "eval_count": 2}),
    })
    backend = OllamaBackend("ollama", "http://127.0.0.1:11434", transport=transport)
    assert backend.health()
    found = backend.discover()[0]
    assert (found.backend_model, found.digest, found.family, found.quantization) == \
        ("local-model:q4", "abcdef1234567890", "llama", "Q4_K_M")
    assert backend.run(NodeCapability.LOCAL_INFERENCE, "local-model:q4", {"text": "hi"},
                       max_output_tokens=4, timeout_seconds=2).content == "hello"
    assert transport.calls[-1][2]["options"] == {"num_predict": 4, "temperature": 0}
    with pytest.raises(BackendError, match="MODEL_NOT_FOUND"):
        backend.run(NodeCapability.LOCAL_INFERENCE, "absent", {"text": "hi"}, max_output_tokens=4, timeout_seconds=2)
    empty = OllamaBackend("ollama", "http://127.0.0.1:11434", transport=FakeTransport({
        ("POST", "api/chat"): (200, {"message": {"content": "", "thinking": "..."}})}))
    with pytest.raises(BackendError, match="EMPTY_COMPLETION"):
        empty.run(NodeCapability.LOCAL_INFERENCE, "m", {"text": "hi"}, max_output_tokens=4, timeout_seconds=2)

    def slow(*_args):
        raise BackendError("BACKEND_TIMEOUT")

    with pytest.raises(BackendError, match="BACKEND_TIMEOUT"):
        OllamaBackend("o", "http://127.0.0.1:11434", transport=slow).run(
            NodeCapability.LOCAL_INFERENCE, "m", {"text": "x"}, max_output_tokens=1, timeout_seconds=0.1)


def test_digest_mismatch_and_missing_model_degrade_the_node(tmp_path, monkeypatch):
    node = make_node(tmp_path, models=[
        {"backend_id": "deterministic", "backend_model": "pados-deterministic-v1", "family": "f", "version": "1",
         "context_limit_tokens": 100, "tasks": ["LOCAL_INFERENCE"], "resource_class": "CPU_LIGHT",
         "privacy_class": "INTERNAL", "digest": "ffffffffffff"},
        {"backend_id": "deterministic", "backend_model": "not-installed", "family": "f", "version": "1",
         "context_limit_tokens": 100, "tasks": ["LOCAL_INFERENCE"], "resource_class": "CPU_LIGHT",
         "privacy_class": "INTERNAL"}], capabilities=["LOCAL_INFERENCE"])
    models = {m["backend_model"]: m["availability"] for m in node.client.get("/models", headers=auth()).json()["models"]}
    assert models == {"pados-deterministic-v1": "DIGEST_MISMATCH", "not-installed": "MISSING"}
    health = node.client.get("/health", headers=auth()).json()
    assert health["state"] == "DEGRADED" and any(r.startswith("CAPABILITY_WITHOUT_AVAILABLE_MODEL") for r in health["reasons"])
    node.runtime.jobs.shutdown()


def test_a_declared_digest_matches_only_by_full_length_equality():
    """Red-team F-9: a short, empty or bare-prefix declared digest never matches (it used to prefix-match)."""
    from pados.compute_node.service import _digest_matches

    full = "a" * 64
    for declared, observed in ((full, full), ("sha256:" + full, full), (full, "sha256:" + full)):
        assert _digest_matches(declared, observed)
    for declared in ("sha256:", "", "a", "a" * 12, "sha256:" + "a" * 63, "A" * 64, "sha256:" + "a" * 65):
        assert not _digest_matches(declared, full), declared
    assert not _digest_matches(full, "a" * 12) and not _digest_matches(full, None)
    assert not _digest_matches(full, "b" * 64)
