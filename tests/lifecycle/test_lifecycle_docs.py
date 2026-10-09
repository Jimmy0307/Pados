import re
from pathlib import Path

from pados.lifecycle.inventory import INVENTORY
from pados.lifecycle.retention import CATALOG
from pados.lifecycle.tables import TABLE_CATALOG

DOCS = Path(__file__).resolve().parents[2] / "docs" / "lifecycle"
_ROW = re.compile(r"^\| `([A-Z0-9_]+)` \| `([A-Z_]+)` \| `(R[0-3]_[A-Z]+)` \| `([A-Z_]+)` \| (`[A-Z0-9_]+`|-) \| (yes|no) \|$")


def test_retention_matrix_matches_catalog() -> None:
    rows = [m.groups() for line in (DOCS / "RETENTION_MATRIX.md").read_text(encoding="utf-8").splitlines()
            if (m := _ROW.match(line))]
    documented = {row[0]: row[1:] for row in rows}
    expected = {
        p.kind.value: (p.category.value, p.retention.value, p.purge_rule.value,
                       f"`{p.payload_retention.value}`" if p.payload_retention else "-",
                       "yes" if p.authoritative else "no")
        for p in CATALOG.values()
    }
    assert documented == expected


def test_retention_matrix_lists_every_table() -> None:
    text = (DOCS / "RETENTION_MATRIX.md").read_text(encoding="utf-8")
    assert all(f"`{table}`" in text for table in TABLE_CATALOG)


def test_lifecycle_doc_covers_contract_and_findings() -> None:
    text = (DOCS / "DATA_LIFECYCLE_V1.md").read_text(encoding="utf-8")
    for term in ("recommendation_only=true", "autonomous_deletion=false", "METADATA_RETAINED_PAYLOAD_PURGED",
                 "PAYLOAD_REFERENCED_BY_ACCEPTED_STATE", "UNSUPPORTED_REQUIRES_RETRAINING_DECISION",
                 "DB_NOT_AVAILABLE", "NOT_CONFIGURED", "eligible_for_purge", "truncated",
                 "ACCEPTED_RISK_WINDOWS", "REFUSED_REFERENCED_AFTER_RESERVE", "POLICY_MISMATCH",
                 "TOMBSTONE_CHAIN_INVALID", "ROOT_SYMLINK_REFUSED", "ESTIMATE_UNAVAILABLE_STAGED_RECORDS_USE_PLAN_PURGE",
                 "PENDING_RECOVERY_REQUIRED", "ProofRejectionAuthority", "REJECTION_TIME_UNKNOWN",
                 "RESTORE_CANONICAL_ALREADY_PRESENT", "RESTORE_CONFLICT", "reserved_ino", "unlink_verified",
                 "state_proof_results_immutable", "RECOVERY_PLAN_MISMATCH", "RESTORE_CLEANUP_FAILED",
                 "LEGACY_PENDING_REQUIRES_OPERATOR", "TOMBSTONE_LEGACY_SCHEMA_READ_ONLY"):
        assert term in text, term
    for point in INVENTORY:
        for finding in point.findings:
            assert finding.split(":")[0] in text
