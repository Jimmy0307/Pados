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


def test_hash_ignores_absent_optional_fields() -> None:
    receipt = _receipt()  # binding_digest and session_ref default to None
    dump = receipt.model_dump(mode="json")
    assert "binding_digest" in dump and dump["binding_digest"] is None  # persisted row still carries the key
    legacy = canonical_hash({key: value for key, value in dump.items() if value is not None})
    assert receipt.receipt_hash == legacy  # the hash is over the non-null projection only


def test_setting_a_used_optional_field_changes_the_hash() -> None:
    # A field that IS used (non-null) is part of the hash, preserving tamper-evidence.
    plain = _receipt()
    with_binding = _receipt(binding_digest="b" * 64)
    assert plain.receipt_hash != with_binding.receipt_hash
