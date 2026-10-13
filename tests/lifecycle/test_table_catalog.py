import re
from pathlib import Path

from pados.lifecycle.retention import RetentionClass, StorageCategory, policy_for
from pados.lifecycle.tables import TABLE_CATALOG

ROOT = Path(__file__).resolve().parents[2]
_CREATE = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?([a-z0-9_]+)")


def _migration_tables() -> set[str]:
    tables: set[str] = set()
    for path in (ROOT / "migrations").rglob("*.py"):
        tables.update(_CREATE.findall(path.read_text(encoding="utf-8")))
    return tables


def test_every_migrated_table_is_mapped_exactly_once() -> None:
    assert _migration_tables() == set(TABLE_CATALOG)


def test_authority_tables_map_to_authority_metadata() -> None:
    for table in ("audit_events", "state_proof_commits", "state_proof_scope_versions", "state_proof_semantic_audit",
                  "state_proof_results", "memory_authority_events", "control_approvals"):
        category, kind = TABLE_CATALOG[table]
        assert category is StorageCategory.AUTHORITY_METADATA
        assert policy_for(kind).retention is RetentionClass.R3_AUTHORITY
    assert TABLE_CATALOG["test_runs"][0] is StorageCategory.RELEASE_TEST_EVIDENCE
    for table, (category, kind) in TABLE_CATALOG.items():
        assert policy_for(kind).category is category, table
