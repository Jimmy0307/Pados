"""docs/machines stays in sync with the code."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pados.machines.inventory import MachineInventory

DOCS = Path(__file__).resolve().parents[2] / "docs" / "machines"


def schema_document() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://pados.invalid/schemas/machines/inventory.v1.json",
        "title": "PADOS machine inventory v1 (pados.machines.inventory.v1)",
        "description": (
            "One operator-owned inventory; one MachineProfile per physical machine. Credentials are references "
            "only (secretref://, ssh-agent://, oscred://). No field may hold a plaintext credential and unknown "
            "fields are refused. Generated from pados.machines.inventory.MachineInventory.model_json_schema(); "
            "the code additionally enforces the per-method reference scheme, canonical host and key forms, the "
            "derived machine_id / fingerprint and the shared assert_minimized guard."),
        **MachineInventory.model_json_schema(),
    }


def test_profile_schema_document_matches_the_model() -> None:
    documented = json.loads((DOCS / "MACHINE_PROFILE_SCHEMA.json").read_text(encoding="utf-8"))
    assert documented == json.loads(json.dumps(schema_document()))
    profile = documented["$defs"]["MachineProfile"]
    assert profile["additionalProperties"] is False
    assert not [name for name in profile["properties"] if "pass" in name or "private" in name]


def test_connection_doc_covers_the_contract() -> None:
    text = (DOCS / "MACHINE_CONNECTION_V1.md").read_text(encoding="utf-8")
    for term in ("PASSWORD_BOOTSTRAP_COMPLETED", "HOST_KEY_MISMATCH", "REMOTE_SESSION_CANNOT_GRANT_PRIVILEGED_OPERATION",
                 "break_glass", "LOCAL_SINGLE_USER", "HERDR_UNAVAILABLE", "AUTH_REQUIRED", "UNAUTHORIZED",
                 "DEGRADED", "OFFLINE", "ONLINE", "STALE", "HostKeyAlias", "ssh-agent://", "oscred://",
                 "secretref://", "pados-machine-identity", "check_compute_nodes", "machine_visibility",
                 "SH-M01", "169.254.0.0/16", "assert_minimized", "OBSERVATION_STALE_REVISION",
                 "GRANT_LEDGER_ROLLBACK_DETECTED", "CREDENTIAL_POLICY_REQUIRED", "ssh -N", "LocalCommand", "PRIVILEGED_AUTHORIZATION_TAMPERED",
                 "GRANT_LEDGER_ISSUE_REQUIRES_PROOFS", "PROBE_RELEVANT_FIELDS", "monotonic anchor",
                 "OperatorProof", "consume_authorization", "ACCEPTED_RISK", "not persisted"):
        assert term in text, term
