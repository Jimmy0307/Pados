"""Stage 12 SecretRef regression tests."""
import json
import logging

import pytest
from enterprise_support import manifest

from pados.enterprise import observability
from pados.enterprise.secrets import (
    REDACTED,
    EnvironmentSecretResolver,
    SecretBoundary,
    is_secret_ref,
    redact,
)
from pados.enterprise.status import enterprise_status
from pados.execution.receipts import execution_receipt

VALUE = "synthetic-secret-value-9f8e7d"
REF = "secretref://env/PADOS_ECP_TEST_TOKEN"


def test_manifest_never_exposes_secret_values():
    item = manifest(secret_refs=(REF,))
    assert VALUE not in item.model_dump_json() and REF in item.model_dump_json()
    assert is_secret_ref(REF) and not is_secret_ref("secretref://env/../x") and not is_secret_ref(VALUE)


def test_secret_resolves_only_inside_authorized_boundary():
    boundary = SecretBoundary(EnvironmentSecretResolver({"PADOS_ECP_TEST_TOKEN": VALUE}))
    empty = SecretBoundary(EnvironmentSecretResolver({}))
    with pytest.raises(PermissionError, match="NOT_AUTHORIZED"), boundary.resolved({REF}, authorized_refs=set()):
        pass
    with (pytest.raises(PermissionError, match="SECRET_REFERENCE_REQUIRED"),
          boundary.resolved({VALUE}, authorized_refs={VALUE})):
        pass
    with pytest.raises(PermissionError, match="UNAVAILABLE"), empty.resolved({REF}, authorized_refs={REF}):
        pass
    with boundary.resolved({REF}, authorized_refs={REF}) as values:
        assert values[REF].reveal() == VALUE
        assert VALUE not in repr(values) and VALUE not in str(values[REF])
        held = values
    assert held == {}  # released when the execution boundary closes


def test_execution_receipt_references_identity_only():
    contract = {"work_contract_id": "wc", "project_id": "p", "secret_refs": [REF]}
    receipt = execution_receipt(run_id="run-1", attempt=1, contract=contract, artifacts=[],
                                provider_id="PADOS_FILE_DIRECT_MAKER", evidence_kind="DETERMINISTIC_HOST", ok=True)
    assert VALUE not in json.dumps(receipt)


def test_failure_logs_and_reports_redact_secret_material(caplog, tmp_path, monkeypatch):
    boundary = SecretBoundary(EnvironmentSecretResolver({"PADOS_ECP_TEST_TOKEN": VALUE}))
    with boundary.resolved({REF}, authorized_refs={REF}) as values:
        message = redact(f"provider failed: credential {VALUE} header Bearer abcdefghijklmnopqrstuv", values)
    assert VALUE not in message and REDACTED in message and "abcdefghijklmnopqrstuv" not in message
    with pytest.raises(ValueError, match="TOO_LONG"):
        observability.emit("provider.completed", reason="x" * 500)
    with pytest.raises(ValueError, match="NOT_ALLOWED"):
        observability.emit("provider.completed", credential_value=VALUE)
    config = tmp_path / "enterprise.json"
    config.write_text(json.dumps({"schema_version": "pados.enterprise.config.v1", "authorized_secret_refs": [REF],
                                  "capabilities": [manifest(secret_refs=(REF,), implementation="pados.enterprise.rerank:NoOpReranker",
                                                            capability_type="RERANK").model_dump(mode="json")]}))
    with caplog.at_level(logging.DEBUG):
        report = enterprise_status(str(config), core_ready=True,
                                   resolver=EnvironmentSecretResolver({"PADOS_ECP_TEST_TOKEN": VALUE}))
    assert VALUE not in json.dumps(report) and VALUE not in caplog.text
    assert report["capabilities"][0]["health"] == "READY"
