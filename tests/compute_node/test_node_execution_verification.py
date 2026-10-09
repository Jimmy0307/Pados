"""Node -> immutable artifact -> SHA-256 -> receipt -> PADOS independent verification."""
import json
import time

import httpx
import pytest
from node_support import PROJECT, json_response, resolver

from pados.compute_node.manifest import NodeCapability
from pados.compute_node.provider import (
    LocalInferenceProvider,
    NodeExecutionError,
    NodeModelBinding,
    RouteIntent,
)
from pados.compute_node.receipts import ReceiptLedger, verify_node_receipt
from pados.enterprise.classification import DataClassification
from pados.enterprise.compute import LocalInferenceProvider as EnterpriseLocalInferenceProvider

PROMPT = "summarize the coolant pump inspection interval"


def _run(node, tmp_path, **kwargs):
    provider = LocalInferenceProvider(node.registry(tmp_path), resolver=resolver(), http=node.http, poll_interval=0.01)
    route = provider.route(RouteIntent(NodeCapability.LOCAL_INFERENCE, DataClassification.INTERNAL, PROJECT))
    values = {"run_id": "run-1", "work_package_id": "wc-1", "inputs": [{"text": PROMPT}], "output_path": "out.md",
              "allowed_paths": ("out.md",), "timeout_seconds": 5}
    values.update(kwargs)
    return provider.execute(route, **values)


def test_authorized_execution_is_independently_verified(node, tmp_path):
    execution = _run(node, tmp_path)
    receipt = execution.receipt
    assert verify_node_receipt(receipt) and receipt["status"] == "SUCCEEDED"
    for key in ("node_id", "provider_id", "model_id", "capability", "work_package_id", "run_id", "started_at",
                "finished_at", "status", "artifacts", "runtime"):
        assert receipt[key] not in (None, "", []), key
    assert receipt["artifacts"][0]["sha256"] == __import__("hashlib").sha256(execution.artifact_bytes).hexdigest()
    assert receipt["verification"]["artifact_sha256"] == "VERIFIED" and receipt["semantic_authority"] is False
    dumped = json.dumps(receipt)
    assert PROMPT not in dumped and "node-cred-" not in dumped and receipt["credential_ref"].startswith("secretref://")
    ledger = ReceiptLedger(tmp_path / "pados-state")
    ledger.append(receipt)
    assert ledger.latest(node.node_id)["receipt_hash"] == receipt["receipt_hash"]
    tampered = {**receipt, "model_id": "other"}
    assert not verify_node_receipt(tampered)
    with pytest.raises(ValueError, match="RECEIPT_INVALID"):
        ledger.append(tampered)


def _rewrite(node, path_part, mutate):
    def rewrite(method, path, response):
        # A job record can arrive from /execute (already terminal) or from /jobs/{id}.
        job_record = path_part == "/jobs/" and path == "/execute"
        if (path_part in path or job_record) and response.status_code < 400:
            return mutate(response)
        return response

    node.rewrite = rewrite


def test_hash_mismatch_is_rejected(node, tmp_path):
    _rewrite(node, "/artifacts/", lambda r: httpx.Response(200, content=b"swapped", headers=dict(r.headers)))
    with pytest.raises(NodeExecutionError, match="ARTIFACT_HASH_MISMATCH"):
        _run(node, tmp_path)


def test_tampered_artifact_on_node_is_rejected(node, tmp_path, monkeypatch):
    original = node.runtime.store.put

    def put_then_tamper(payload, **kwargs):
        ref = original(payload, **kwargs)
        target = node.runtime.store.root / ref.sha256
        target.chmod(0o644)
        target.write_bytes(b"tampered")
        return ref

    monkeypatch.setattr(node.runtime.store, "put", put_then_tamper)
    with pytest.raises(NodeExecutionError, match="ARTIFACT_TAMPERED"):
        _run(node, tmp_path)


def test_missing_and_unexpected_artifacts_are_rejected(node, tmp_path):
    def with_mac(body, credential="node-cred-4f1c9a7e2b6d8053"):
        from pados.compute_node.identity import record_mac

        body["record_mac"] = record_mac(credential, body)

    _rewrite(node, "/jobs/", lambda r: json_response(r, lambda b: (b.update(artifacts=[]), with_mac(b))
                                                      if b["state"] == "SUCCEEDED" else None))
    with pytest.raises(NodeExecutionError, match="ARTIFACT_MISSING"):
        _run(node, tmp_path)
    _rewrite(node, "/jobs/", lambda r: json_response(r, lambda b: (b.update(artifacts=b["artifacts"] * 2), with_mac(b))
                                                      if b["state"] == "SUCCEEDED" else None))
    with pytest.raises(NodeExecutionError, match="ARTIFACT_UNEXPECTED"):
        _run(node, tmp_path)
    node.rewrite = None
    _rewrite(node, "/artifacts/", lambda r: httpx.Response(404, json={"detail": "ARTIFACT_NOT_FOUND"}))
    with pytest.raises(NodeExecutionError, match="ARTIFACT_MISSING"):
        _run(node, tmp_path)


def test_unauthorized_artifact_path_is_rejected(node, tmp_path):
    with pytest.raises(NodeExecutionError, match="ARTIFACT_PATH_UNAUTHORIZED"):
        _run(node, tmp_path, output_path="../escape.md")

    def repath(body, credential="node-cred-4f1c9a7e2b6d8053"):
        from pados.compute_node.identity import record_mac

        if body["state"] == "SUCCEEDED":
            body["artifacts"][0]["path"] = "other.md"
            body["output_path"] = "out.md"
            body["record_mac"] = record_mac(credential, body)

    _rewrite(node, "/jobs/", lambda r: json_response(r, repath))
    with pytest.raises(NodeExecutionError, match="ARTIFACT_PATH_UNAUTHORIZED"):
        _run(node, tmp_path)


def test_invalid_receipt_is_rejected(node, tmp_path):
    # Forge every job record the client can see: /execute may already return a terminal record.
    def forge(method, path, response):
        if (path == "/execute" or "/jobs/" in path) and response.status_code < 400:
            return json_response(response, lambda b: b.update(model_id="forged"))
        return response

    node.rewrite = forge
    with pytest.raises(NodeExecutionError, match="NODE_RECEIPT_INVALID"):
        _run(node, tmp_path)


def test_stale_manifest_is_rejected(node, tmp_path):
    provider = LocalInferenceProvider(node.registry(tmp_path), resolver=resolver(), http=node.http, poll_interval=0.01)
    route = provider.route(RouteIntent(NodeCapability.LOCAL_INFERENCE, DataClassification.INTERNAL, PROJECT))
    node.runtime.config.limits.__dict__["max_batch_items"] = 7  # node manifest changes after routing
    with pytest.raises(NodeExecutionError, match="NODE_MANIFEST_STALE"):
        provider.execute(route, run_id="r", work_package_id="w", inputs=[{"text": "x"}], output_path="o.md",
                         allowed_paths=("o.md",), timeout_seconds=5)


def _until_idle(node, timeout=5.0):
    deadline = time.monotonic() + timeout
    while node.runtime.jobs.load().running and time.monotonic() < deadline:
        time.sleep(0.02)
    assert node.runtime.jobs.load().running == 0


def test_timeout_cancellation_and_endpoint_failure(node, tmp_path):
    with pytest.raises(NodeExecutionError, match="JOB_TIMED_OUT"):
        _run(node, tmp_path, inputs=[{"text": "slow", "simulate_latency_ms": 2000}], timeout_seconds=1)
    _until_idle(node)  # the timed-out job's worker still holds its slot until its backend call returns
    _rewrite(node, "/execute", lambda r: httpx.Response(503, json={"detail": "NODE_UNAVAILABLE"}))
    with pytest.raises(NodeExecutionError, match="NODE_UNAVAILABLE"):
        _run(node, tmp_path)
    _until_idle(node)  # the node admitted that job; only the response was replaced

    def cancelled(body):
        body["state"] = "CANCELLED"

    node.rewrite = None
    original = node.runtime.jobs.submit

    def submit_then_cancel(request):
        record = original(request)
        return node.runtime.jobs.cancel(record["job_id"])

    node.runtime.jobs.submit = submit_then_cancel
    with pytest.raises(NodeExecutionError, match="JOB_CANCELLED"):
        _run(node, tmp_path)


def test_node_model_binding_satisfies_enterprise_local_inference_protocol(node, tmp_path):
    provider = LocalInferenceProvider(node.registry(tmp_path), resolver=resolver(), http=node.http, poll_interval=0.01)
    route = provider.route(RouteIntent(NodeCapability.LOCAL_INFERENCE, DataClassification.INTERNAL, PROJECT))
    binding: EnterpriseLocalInferenceProvider = NodeModelBinding(provider, route)
    assert binding.profile.locality.value == "LOCAL" and binding.health().value == "READY"
    assert binding.generate("hello local node", max_tokens=8).startswith("Deterministic local completion")
