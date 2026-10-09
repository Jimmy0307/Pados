from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "migrations" / "versions"
MIGRATION_0010 = VERSIONS / "20260730_0010_development_ledger.py"
MIGRATION_0011 = VERSIONS / "20260730_0011_knowledge_corpus_pilot.py"
MIGRATION_0012 = VERSIONS / "20260730_0012"


def _script_directory() -> ScriptDirectory:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def _created_tables(path: Path) -> set[str]:
    return set(re.findall(r"CREATE TABLE(?: IF NOT EXISTS)? ([a-z_]+)", path.read_text(encoding="utf-8")))


def _dropped_tables(path: Path) -> set[str]:
    source = path.read_text(encoding="utf-8").split("def downgrade", 1)[-1]
    return set(re.findall(r'"([a-z_]+)"', source)) | set(re.findall(r"DROP TABLE IF EXISTS ([a-z_]+)", source))


def test_0010_exists_with_authoritative_lineage() -> None:
    assert MIGRATION_0010.is_file()
    source = MIGRATION_0010.read_text(encoding="utf-8")
    assert 'revision: str = "20260730_0010"' in source
    assert 'down_revision: str | None = "20260711_0009"' in source
    assert "0012" not in source


def test_0011_exists_with_0010_parent_and_no_ui_preview_schema() -> None:
    assert MIGRATION_0011.is_file()
    source = MIGRATION_0011.read_text(encoding="utf-8")
    assert 'revision: str = "20260730_0011"' in source
    assert 'down_revision: str | None = "20260730_0010"' in source
    assert "ui_" + "specification_" + "previews" not in source


def test_script_directory_resolves_exact_chain() -> None:
    script = _script_directory()
    assert script.get_revision("20260711_0009").revision == "20260711_0009"
    assert script.get_revision("20260730_0010").down_revision == "20260711_0009"
    assert script.get_revision("20260730_0011").down_revision == "20260730_0010"
    assert script.get_revision("20260730_0011").revision == "20260730_0011"
    assert {revision.revision for revision in script.walk_revisions("20260711_0009", "20260730_0011")} == {
        "20260711_0009",
        "20260730_0011",
        "20260730_0010",
    }


def test_0012_is_absent_and_runtime_does_not_select_head() -> None:
    assert not MIGRATION_0012.exists()
    candidates = [ROOT / "Start-PADOS-P6-15-Corpus.ps1", ROOT / "scripts", ROOT / "src" / "pados" / "corpus"]
    source = "\n".join(path.read_text(encoding="utf-8") if path.is_file() else "\n".join(item.read_text(encoding="utf-8") for item in path.rglob("*.py")) for path in candidates)
    assert "upgrade " + "head" not in source


def test_upgrade_and_downgrade_table_sets_are_symmetric() -> None:
    for migration in (MIGRATION_0010, MIGRATION_0011):
        created = _created_tables(migration)
        dropped = _dropped_tables(migration)
        assert created <= dropped, (migration.name, sorted(created - dropped))
