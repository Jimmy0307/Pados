"""Scenario A/E against real authority: proof-lane receipts survive an offline period,
reconnect sees the original artifact, approval commits once through CAS with audit."""
from __future__ import annotations

from sqlalchemy import text

from pados.connectivity.adapter import SimulatedConnectivity
from pados.remote.approval import RemoteApprovalOutcome
from pados.state_proof import repository
from tests.integration.test_remote_live_ports import (  # noqa: F401
    _audit,
    _decide,
    _human_gated,
    remote_live,
)


def test_offline_receipt_survives_and_reconnect_commits_once(remote_live) -> None:  # noqa: F811
    service, ports, engine = remote_live
    net = SimulatedConnectivity()
    net.go_offline()
    run_id, scope_id = _human_gated(service)  # local execution while WAN/overlay are down
    before = ports.scope_version(scope_id)
    with service.state_gateway.proof_service._session() as session:
        results = repository.get_results_for_run(session, run_id)  # receipts written while offline
    assert results and all(r["status"] == "SUCCEEDED" for r in results)
    offline_view = ports.run_summary(run_id)
    assert offline_view is not None and offline_view["artifacts"]
    original = offline_view["artifacts"]
    assert original[0]["completed_at"] and original[0]["output_content_hash"]
    assert _audit(engine, scope_id) == [] and ports.scope_version(scope_id) == before  # no auto-approval

    net.recover()
    assert ports.run_summary(run_id)["artifacts"] == original  # type: ignore[index]
    result, ref = _decide(ports, run_id, scope_id)
    assert result.outcome is RemoteApprovalOutcome.COMMITTED
    assert ports.scope_version(scope_id) == before + 1
    assert _audit(engine, scope_id) == [{"actor": "REMOTE:owner", "approval_ref": ref}]
    with engine.connect() as connection:
        commits = connection.execute(text("SELECT count(*) FROM state_proof_commits WHERE result_id::text = :r"),
                                     {"r": original[0]["receipt_id"]}).scalar()
    assert commits == 1
    assert ports.presented_decision(ref) is None  # approval no longer pending: cannot be reused
