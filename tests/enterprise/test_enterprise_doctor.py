import json

import pytest
from enterprise_support import manifest
from typer.testing import CliRunner

from pados.enterprise.classification import DataClassification
from pados.enterprise.manifest import CapabilityType, ExecutionMode
from pados.enterprise.status import enterprise_status
from pados.enterprise.visibility import enterprise_visibility

REF = "secretref://env/PADOS_ECP_DOCTOR_TOKEN"


def _config(tmp_path, entries, **extra):
    path = tmp_path / "enterprise.json"
    path.write_text(json.dumps({"schema_version": "pados.enterprise.config.v1", **extra,
                                "capabilities": [e.model_dump(mode="json") for e in entries]}))
    return str(path)


def test_unconfigured_extension_does_not_affect_core_readiness():
    report = enterprise_status("", core_ready=True)
    assert report == {"core": "CORE_READY", "core_readiness_affected": False,
                      "status": "ENTERPRISE_EXTENSION_NOT_CONFIGURED", "capabilities": []}


def test_doctor_distinguishes_every_enterprise_state(tmp_path):
    entries = [
        manifest("knowledge.local", implementation="pados.enterprise.pipeline:local_pipeline"),
        manifest("ocr.local", CapabilityType.OCR, implementation="pados.enterprise.extraction:UnconfiguredOcrExtractor"),
        manifest("rerank.missing", CapabilityType.RERANK, implementation="pados.enterprise.rerank:DoesNotExist"),
        manifest("rerank.remote", CapabilityType.RERANK, execution_mode=ExecutionMode.REMOTE_HTTP,
                 implementation="pados.enterprise.rerank:NoOpReranker"),
        manifest("rerank.secret", CapabilityType.RERANK, secret_refs=(REF,),
                 implementation="pados.enterprise.rerank:NoOpReranker"),
        manifest("rerank.degraded", CapabilityType.RERANK, health="DEGRADED",
                 implementation="pados.enterprise.rerank:DeterministicLexicalReranker"),
    ]
    report = enterprise_status(_config(tmp_path, entries), core_ready=True)
    states = {row["capability_id"]: row["state"] for row in report["capabilities"]}
    assert states == {
        "knowledge.local": "ENTERPRISE_CAPABILITY_READY",
        "ocr.local": "ENTERPRISE_CAPABILITY_NOT_CONFIGURED",
        "rerank.missing": "ENTERPRISE_CAPABILITY_UNAVAILABLE",
        "rerank.remote": "ENTERPRISE_CAPABILITY_UNAUTHORIZED",
        "rerank.secret": "ENTERPRISE_CAPABILITY_UNAUTHORIZED",
        "rerank.degraded": "ENTERPRISE_CAPABILITY_DEGRADED",
    }
    assert report["core"] == "CORE_READY" and report["status"] == "ENTERPRISE_EXTENSION_DEGRADED"
    assert report["core_readiness_affected"] is False


def test_authorized_but_unresolvable_secret_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.delenv("PADOS_ECP_DOCTOR_TOKEN", raising=False)
    entry = manifest("rerank.secret", CapabilityType.RERANK, secret_refs=(REF,),
                     implementation="pados.enterprise.rerank:NoOpReranker")
    report = enterprise_status(_config(tmp_path, [entry], authorized_secret_refs=[REF]), core_ready=False)
    assert report["capabilities"][0]["state"] == "ENTERPRISE_CAPABILITY_UNAVAILABLE"
    assert report["core"] == "CORE_NOT_READY"


@pytest.mark.parametrize("reference", ["os:system", "subprocess:Popen", "pados.state_proof.service:StateProofService"])
def test_configuration_cannot_import_arbitrary_code(tmp_path, reference):
    entry = manifest("rerank.x", CapabilityType.RERANK, implementation=reference)
    report = enterprise_status(_config(tmp_path, [entry]), core_ready=True)
    assert report["status"] == "ENTERPRISE_EXTENSION_INVALID"
    assert report["reason"] == "ENTERPRISE_IMPLEMENTATION_NOT_ALLOWED"


def test_invalid_configuration_is_reported_not_raised(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert enterprise_status(str(bad), core_ready=True)["status"] == "ENTERPRISE_EXTENSION_INVALID"
    assert enterprise_status(str(tmp_path / "missing.json"), core_ready=True)["reason"] == "FileNotFoundError"


def test_existing_doctor_reports_enterprise_section_without_changing_exit(monkeypatch, tmp_path):
    from pados.cli import main as cli
    from pados.config import get_settings

    monkeypatch.setenv("PADOS_DB_PORT", "9")  # Core not ready here; the extension must not change that
    monkeypatch.delenv("PADOS_ENTERPRISE_CONFIG", raising=False)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli.app, ["doctor", "--json"])
    finally:
        get_settings.cache_clear()
    payload, _ = json.JSONDecoder().raw_decode(result.output[result.output.index("{"):])
    assert payload["enterprise"]["status"] == "ENTERPRISE_EXTENSION_NOT_CONFIGURED"
    assert payload["enterprise"]["core"] == ("CORE_READY" if payload["readiness"]["ready"] else "CORE_NOT_READY")
    assert result.exit_code == (0 if payload["status"] == "ok" else 1)


def test_mission_control_visibility_is_projection_only(tmp_path):
    report = enterprise_visibility(config_path="", core_ready=True, object_root=str(tmp_path), known_projects=["p"],
                                   rows=lambda _sql: [])
    assert report["status"] == "ENTERPRISE_EXTENSION_NOT_CONFIGURED" and report["projection_only"] is True
    assert report["semantic_authority"] == "NONE"

    def broken(_sql):
        raise RuntimeError("PROOF_AUTHORITY_UNCONFIGURED")

    degraded = enterprise_visibility(config_path="", core_ready=False, object_root="", known_projects=[], rows=broken)
    assert degraded["authorization_failures"] == [{"kind": "PROOF_LANE_UNAVAILABLE", "reason": "RuntimeError"}]


def test_restricted_classification_is_declared_by_manifest():
    assert manifest(privacy_classes=(DataClassification.PUBLIC,)).max_classification is DataClassification.PUBLIC
