import unittest

from pados.state_proof.audit_chain import build_audit_record, verify_audit_chain


class AuditChainTests(unittest.TestCase):
    def record(self, **overrides):
        values = {
            "scope_id": "project-1",
            "actor": "PADOS_PRODUCT_LOOP",
            "action": "SEMANTIC_COMMIT",
            "target": "scope:project-1",
            "reason": "deterministic validation passed",
            "timestamp": "2026-09-23T10:00:00+00:00",
            "request_id": "run-1",
            "idempotency_key": "request-1",
            "outcome": "COMMITTED",
            "before_state": {"scope_version": 4},
            "after_state": {"scope_version": 5},
            "approval_ref": None,
            "rollback_ref": None,
            "previous_hash": None,
        }
        values.update(overrides)
        return build_audit_record(**values)

    def test_valid_hash_chain_with_required_write_metadata(self):
        first = self.record()
        second = self.record(
            timestamp="2026-09-23T10:01:00+00:00",
            request_id="run-2",
            idempotency_key="request-2",
            previous_hash=first["record_hash"],
            before_state={"scope_version": 5},
            after_state={"scope_version": 6},
            approval_ref="approval-2",
            rollback_ref="rollback-plan-2",
        )
        self.assertTrue(verify_audit_chain([first, second]))
        self.assertEqual(second["previous_hash"], first["record_hash"])
        self.assertEqual(second["approval_ref"], "approval-2")

    def test_tampered_record_or_link_breaks_chain(self):
        first = self.record()
        second = self.record(
            timestamp="2026-09-23T10:01:00+00:00",
            request_id="run-2",
            idempotency_key="request-2",
            previous_hash=first["record_hash"],
        )
        tampered = {**second, "after_state": {"scope_version": 99}}
        self.assertFalse(verify_audit_chain([first, tampered]))
        broken_link = {**second, "previous_hash": "0" * 64}
        self.assertFalse(verify_audit_chain([first, broken_link]))

    def test_duplicate_idempotency_identity_is_invalid(self):
        first = self.record()
        second = self.record(
            timestamp="2026-09-23T10:01:00+00:00",
            request_id="run-2",
            previous_hash=first["record_hash"],
        )
        self.assertFalse(verify_audit_chain([first, second]))


if __name__ == "__main__":
    unittest.main()
