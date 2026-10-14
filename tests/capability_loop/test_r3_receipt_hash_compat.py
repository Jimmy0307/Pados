# SPDX-License-Identifier: AGPL-3.0-only
"""Round-3 Finding 1: adding an optional receipt field must not change existing receipt hashes.

The receipt hash is computed over the non-null projection, so a receipt that does not use a newly added
optional field (binding_digest / session_ref) hashes the same whether that field is absent or present as
null. A store written before a field existed therefore still verifies after the upgrade.
"""
from __future__ import annotations

from pados.capability_loop.receipts import GENESIS, OperationKind, OperationReceipt, OperationResult
from pados.execution.context import canonical_hash


def _receipt(**overrides: object) -> OperationReceipt:
    base = dict(sequence=1, previous_receipt_hash=GENESIS, operation=OperationKind.DISCOVER,
                project_id="proj-a", request_digest="a" * 64, provider_id="rea.capability_discovery",
                capability_id="BINARY_INSPECTION", policy_code="ALLOWED",
                result=OperationResult.RECORDED, accepted_into_loop=True, recorded_at="2026-10-14T00:00:00")
    base.update(overrides)
    return OperationReceipt(**base)  # type: ignore[arg-type]


def test_hash_drops_only_the_new_fields_and_preserves_legacy_projection() -> None:
    receipt = _receipt()  # binding_digest and session_ref default to None
    dump = receipt.model_dump(mode="json")
    assert dump["failure"] is None and dump["runtime_version"] is None  # other optional fields are null
    # The legacy hash is the full model dump WITHOUT the two newly-added fields, keeping every original
    # field (including its null). The upgraded receipt must hash identically.
    legacy = {key: value for key, value in dump.items() if key not in ("binding_digest", "session_ref")}
    assert "failure" in legacy and "runtime_version" in legacy  # original nulls stay in the projection
    assert receipt.receipt_hash == canonical_hash(legacy)


def test_setting_a_used_optional_field_changes_the_hash() -> None:
    # A field that IS used (non-null) is part of the hash, preserving tamper-evidence.
    plain = _receipt()
    with_binding = _receipt(binding_digest="b" * 64)
    assert plain.receipt_hash != with_binding.receipt_hash
