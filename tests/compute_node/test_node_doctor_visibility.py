import json
import logging

from node_support import CREDENTIAL_VALUE, resolver
from typer.testing import CliRunner

from pados.compute_node.manifest import NodeCapability
from pados.compute_node.provider import LocalInferenceProvider, RouteIntent
from pados.compute_node.status import compute_nodes_status
from pados.compute_node.visibility import compute_node_visibility
from pados.enterprise.classification import DataClassification


def _config(tmp_path, node, *registrations):
    path = tmp_path / "nodes.json"
    registry = node.registry(tmp_path, *registrations)
    path.write_text(registry.model_dump_json())
    return str(path)


def test_unconfigured_compute_nodes_do_not_affect_core():
    assert compute_nodes_status("", core_ready=True) == {
        "core": "CORE_READY", "core_readiness_affected": False, "status": "COMPUTE_NODES_NOT_CONFIGURED", "nodes": []}
    assert compute_node_visibility("")["status"] == "COMPUTE_NODES_NOT_CONFIGURED"


def test_doctor_checks_every_dimension_and_never_logs_credentials(node, tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        report = compute_nodes_status(_config(tmp_path, node), core_ready=True, resolver=resolver(), http=node.http)
    checks = report["nodes"][0]["checks"]
    assert report["status"] == "COMPUTE_NODES_READY" and report["core_readiness_affected"] is False
    assert checks == {"configuration": "OK", "endpoint": "OK", "authentication": "OK", "health": "ONLINE",
                      "capability_manifest": "OK", "model_availability": "1/1",
                      "provider_compatibility": ["CLASSIFICATION", "EMBEDDING", "LOCAL_INFERENCE"]}
    assert CREDENTIAL_VALUE not in json.dumps(report) and CREDENTIAL_VALUE not in caplog.text
    unauthorized = compute_nodes_status(_config(tmp_path, node, node.registration(authorized=False)),
                                        core_ready=False, resolver=resolver(), http=node.http)
    assert unauthorized["status"] == "COMPUTE_NODES_DEGRADED" and unauthorized["core"] == "CORE_NOT_READY"
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert compute_nodes_status(str(bad), core_ready=True)["status"] == "COMPUTE_NODES_INVALID"


def test_mission_control_projection_uses_persisted_evidence(node, tmp_path):
    config = _config(tmp_path, node)
    compute_nodes_status(config, core_ready=True, resolver=resolver(), http=node.http)
    provider = LocalInferenceProvider(node.registry(tmp_path), resolver=resolver(), http=node.http, poll_interval=0.01)
    route = provider.route(RouteIntent(NodeCapability.LOCAL_INFERENCE, DataClassification.INTERNAL, "project-alpha"))
    execution = provider.execute(route, run_id="run-9", work_package_id="wc-9", inputs=[{"text": "x"}],
                                 output_path="o.md", allowed_paths=("o.md",), timeout_seconds=5)
    from pados.compute_node.receipts import ReceiptLedger

    ReceiptLedger(tmp_path / "pados-state").append(execution.receipt)
    view = compute_node_visibility(config)
    entry = view["nodes"][0]
    assert entry["state"] == "ONLINE" and entry["platform"] and entry["execution_class"] == "LOCAL"
    assert entry["models"][0]["availability"] == "AVAILABLE" and entry["last_successful_check"]
    assert entry["last_execution"]["run_id"] == "run-9" and entry["current_load"]["max_concurrent_jobs"] == 1
    assert view["projection_only"] is True


def test_existing_doctor_reports_compute_nodes_without_changing_exit(monkeypatch):
    from pados.cli import main as cli
    from pados.config import get_settings

    monkeypatch.setenv("PADOS_DB_PORT", "9")
    monkeypatch.delenv("PADOS_COMPUTE_NODES_CONFIG", raising=False)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli.app, ["doctor", "--json"])
    finally:
        get_settings.cache_clear()
    payload, _ = json.JSONDecoder().raw_decode(result.output[result.output.index("{"):])
    assert payload["compute_nodes"]["status"] == "COMPUTE_NODES_NOT_CONFIGURED"
    assert result.exit_code == (0 if payload["status"] == "ok" else 1)
