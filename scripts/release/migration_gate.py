# SPDX-License-Identifier: AGPL-3.0-only
"""Migration head gate: declared operational heads == actual Alembic heads == fresh-install migrated heads.

Usage:
  migration_gate.py OUT.json                 offline: declared revisions vs Alembic script heads
  migration_gate.py OUT.json --fresh-install also bootstrap fresh disposable lane databases through the
                                             installer's own upgrade path and read back alembic_version

Declared revisions are the runtime's own defaults (``pados.config``): the operational lane, the corpus lane and
the proof lane. Nothing in CI pins a revision id; adding a migration without moving the declared head (or the
reverse) fails this gate. ``--fresh-install`` needs a disposable loopback PostgreSQL named by the usual
``PADOS_DB_*`` variables and refuses any lane database that already exists, so it can never touch real state.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from pados import config as pados_config  # noqa: E402
from pados.release.supply_chain import migration_heads  # noqa: E402

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


def offline_findings() -> tuple[dict[str, Any], list[str]]:
    heads = migration_heads(ROOT)
    declared = {
        "operational": sorted({pados_config.OPERATIONAL_REVISION_DEFAULT, pados_config.CORPUS_REVISION_DEFAULT}),
        "proof": [pados_config.PROOF_REVISION_HEAD],
    }
    findings = [
        f"DECLARED_HEADS_MISMATCH:{lane}:declared={declared[lane]}:alembic={heads[lane]['heads']}"
        for lane in sorted(declared)
        if declared[lane] != heads[lane]["heads"]
    ]
    return {"declared": declared, "alembic_heads": {lane: heads[lane]["heads"] for lane in heads}}, findings


def fresh_install_findings() -> tuple[dict[str, Any], list[str]]:
    from sqlalchemy import create_engine, text

    from pados.config import get_settings, lane_settings
    from pados.install import database

    settings = get_settings()
    if settings.db_host not in LOOPBACK:
        return {}, ["FRESH_INSTALL_REQUIRES_LOOPBACK_DATABASE"]
    lanes = database.managed_lanes(settings)
    maintenance = create_engine(lane_settings(settings, "postgres").database_url, isolation_level="AUTOCOMMIT")
    try:
        with maintenance.connect() as connection:
            existing = [lane.database for lane in lanes if connection.execute(
                text("select 1 from pg_database where datname = :name"), {"name": lane.database}).scalar()]
    finally:
        maintenance.dispose()
    if existing:
        return {"existing": existing}, ["FRESH_INSTALL_DATABASE_ALREADY_EXISTS"]
    created = database.create_missing_databases(settings)
    result = database.upgrade(settings)
    check = database.check(settings)
    migrated = {item["lane"]: item.get("revisions") for item in check["lanes"]}
    declared = {lane.name: [lane.target_revision] for lane in lanes}
    findings: list[str] = []
    if result.get("status") != "CURRENT" or check.get("status") != "CURRENT":
        findings.append(f"FRESH_INSTALL_NOT_CURRENT:{result.get('status')}:{check.get('status')}")
    findings.extend(
        f"FRESH_INSTALL_HEAD_MISMATCH:{lane}:declared={declared[lane]}:migrated={migrated.get(lane)}"
        for lane in sorted(declared)
        if [str(item) for item in (migrated.get(lane) or [])] != declared[lane]
    )
    return {"created": created, "declared": declared, "migrated": migrated,
            "applied": result.get("applied", [])}, findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--fresh-install", action="store_true")
    args = parser.parse_args(argv)
    offline, findings = offline_findings()
    report: dict[str, Any] = {"offline": offline}
    if args.fresh_install:
        fresh, fresh_findings = fresh_install_findings()
        report["fresh_install"] = fresh
        findings.extend(fresh_findings)
    report["findings"] = findings
    report["status"] = "PASS" if not findings else "FAIL"
    Path(args.out).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for finding in findings:
        print(f"FINDING {finding}")
    print("MIGRATION_GATE_OK" if not findings else f"MIGRATION_GATE_FINDINGS={len(findings)}")
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
