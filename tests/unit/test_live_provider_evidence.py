"""Live provider readiness derives only from verified, accepted, fresh evidence."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pados.execution.receipts import execution_receipt
from pados.operations.live_evidence import evaluate_live_evidence

NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
PROVIDER = "PADOS_CODEX_CLI"
CONTRACT = {"work_contract_id": "contract-1", "objective": "append log"}
ARTIFACTS = [{"path": "docs/log.md", "sha256": "a" * 64, "size_bytes": 10}]


def _manifest(*, kind: str = "LIVE_PROVIDER", provider: str = PROVIDER, status: str = "COMPLETED",
              tamper: bool = False) -> dict[str, Any]:
    receipt = execution_receipt(run_id="run-1", attempt=1, contract=CONTRACT, artifacts=ARTIFACTS,
                                provider_id=provider, evidence_kind=kind, ok=True)
    if tamper:
        receipt = {**receipt, "artifacts": [{**ARTIFACTS[0], "sha256": "b" * 64}]}
    return {"specification": {"work_contract": CONTRACT, "runtime_result": {
        "run_id": "run-1", "attempt": 1, "status": status, "artifact_refs": ARTIFACTS, "execution_receipt": receipt}}}


def _row(age: timedelta = timedelta(hours=1), **overrides: Any) -> dict[str, Any]:
    return {"result_id": "result-1", "provider_id": PROVIDER, "result_status": "SUCCEEDED",
            "commit_status": "COMMITTED", "accepted_scope_version": 1, "accepted_at": NOW - age, **overrides}


def _evaluate(rows: list[dict[str, Any]], manifest: dict[str, Any] | None) -> dict[str, Any]:
    return evaluate_live_evidence(rows, provider_id=PROVIDER, now=NOW, read_manifest=lambda _row: manifest)


def test_no_live_evidence_is_pending() -> None:
    assert _evaluate([], None)["status"] == "LIVE_UAT_PENDING"


def test_stale_verified_evidence_is_stale_not_verified() -> None:
    result = _evaluate([_row(age=timedelta(days=30))], _manifest())
    assert result["status"] == "LIVE_UAT_STALE"


def test_failed_execution_or_rejected_commit_never_counts() -> None:
    assert _evaluate([_row(result_status="FAILED")], _manifest())["status"] == "LIVE_UAT_UNVERIFIED"
    assert _evaluate([_row(commit_status="STALE_CANDIDATE")], _manifest())["status"] == "LIVE_UAT_UNVERIFIED"
    assert _evaluate([_row()], _manifest(status="FAILED"))["status"] == "LIVE_UAT_UNVERIFIED"


def test_unverified_simulated_or_foreign_receipts_never_count() -> None:
    assert _evaluate([_row()], _manifest(tamper=True))["status"] == "LIVE_UAT_UNVERIFIED"
    assert _evaluate([_row()], _manifest(kind="SIMULATION"))["status"] == "LIVE_UAT_UNVERIFIED"
    assert _evaluate([_row()], _manifest(kind="DETERMINISTIC_HOST"))["status"] == "LIVE_UAT_UNVERIFIED"
    assert _evaluate([_row()], _manifest(provider="OTHER"))["status"] == "LIVE_UAT_UNVERIFIED"
    assert _evaluate([_row()], None)["status"] == "LIVE_UAT_UNVERIFIED"


def test_valid_recent_live_codex_evidence_is_verified() -> None:
    result = _evaluate([_row()], _manifest())
    assert result["status"] == "LIVE_UAT_VERIFIED"
    assert result["evidence"]["provider_id"] == PROVIDER
    assert result["evidence"]["execution_receipt"].startswith("execution-sha256:")
