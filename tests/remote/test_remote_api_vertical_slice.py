from __future__ import annotations

from pathlib import Path

from pados.config import Settings

from .remote_slice_support import PROJECT, build_slice


def test_start_task_submits_intent_and_reconstructs_context_server_side(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    body = {"intent_id": "i-start-1", "project_id": PROJECT, "intent_type": "START_TASK",
            "human_message": "tighten the README"}
    first = s.client.post("/remote/v1/intents", json=body, headers=s.owner_headers)
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["outcome"] == "SUBMITTED"
    assert data["context"]["context_source"] == "PADOS_AUTHORITATIVE_STORE"
    assert data["context"]["client_history_included"] is False
    replay = s.client.post("/remote/v1/intents", json=body, headers=s.owner_headers).json()
    assert replay["run_id"] == data["run_id"]  # idempotent on intent_id
    work_map = s.client.get("/remote/v1/work-map", headers=s.owner_headers)
    assert work_map.status_code == 200
    assert work_map.json()["projects"] == [PROJECT]
    task = work_map.json()["tasks"][0]
    assert task["original_intent"] == body["human_message"]
    assert task["decomposition_status"] == "PENDING"
    assert task["goals"][0]["objective"] == body["human_message"]
    assert task["assignments"] == []
    assert work_map.json()["projection"] == "REBUILDABLE_PADOS_PROJECTION_NOT_AUTHORITY"
    assert s.service.ledger.verify()


def test_work_map_requires_authentication_and_filters_to_actor_projects(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    assert s.client.get("/remote/v1/work-map").status_code == 401
    payload = {"intent_id": "i-map-1", "project_id": PROJECT, "intent_type": "START_TASK",
               "human_message": "show this task"}
    assert s.client.post("/remote/v1/intents", json=payload, headers=s.owner_headers).status_code == 200
    view = s.client.get("/remote/v1/work-map", headers=s.viewer_headers).json()
    assert [task["project_id"] for task in view["tasks"]] == [PROJECT]
    assert s.client.get(
        "/remote/v1/work-map", headers={"X-PADOS-Remote-Actor": "viewer", "X-PADOS-Remote-Proof": "wrong"}
    ).status_code == 401


def test_static_frontend_cors_uses_exact_origin_allowlist(tmp_path: Path) -> None:
    s = build_slice(tmp_path, allowed_origins=("https://jimmy0307.github.io",))
    allowed = s.client.options(
        "/remote/v1/work-map",
        headers={
            "Origin": "https://jimmy0307.github.io",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-pados-remote-actor,x-pados-remote-proof",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://jimmy0307.github.io"
    assert "access-control-allow-credentials" not in allowed.headers
    denied = s.client.options(
        "/remote/v1/work-map",
        headers={"Origin": "https://attacker.example", "Access-Control-Request-Method": "GET"},
    )
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


def test_client_supplied_context_is_rejected_over_http(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    body = {"intent_id": "i-2", "project_id": PROJECT, "intent_type": "START_TASK",
            "human_message": "x", "chat_history": ["..."]}
    response = s.client.post("/remote/v1/intents", json=body, headers=s.owner_headers)
    assert response.status_code == 422
    assert response.json()["detail"] == "CLIENT_CONTEXT_FORBIDDEN"


def test_remote_approval_commits_through_cas(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    run_id = s.human_gated_run()
    attention = s.client.get("/remote/v1/attention", headers=s.owner_headers).json()
    pending = attention["waiting_approval"]
    assert [row["run_id"] for row in pending] == [run_id]
    assert s.gateway.versions.get(PROJECT, 0) == 0
    ref = pending[0]["approval_ref"]
    # A standing authenticated session alone is refused: no step-up proof.
    unproven = s.client.post(
        f"/remote/v1/approvals/{ref}/approve",
        json={"intent_id": "i-approve-0", "project_id": PROJECT, "target_run_id": run_id},
        headers=s.owner_headers,
    )
    assert unproven.status_code == 422 and unproven.json()["detail"] == "APPROVAL_STEP_UP_PROOF_REQUIRED"
    headers, proof = s.step_up(ref)
    decision = s.client.post(
        f"/remote/v1/approvals/{ref}/approve",
        json={"intent_id": "i-approve", "project_id": PROJECT, "target_run_id": run_id, "step_up_proof": proof},
        headers=headers,
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["outcome"] == "COMMITTED"
    assert decision.json()["step_up_proof_ref"] == proof
    assert s.gateway.versions[PROJECT] == 1
    assert s.ports.commit_calls[0]["expected_version"] == 0
    assert s.ports.commit_calls[0]["actor"] == "owner"
    assert s.ports.commit_calls[0]["proof_ref"] == proof
    after = s.client.get("/remote/v1/attention", headers=s.owner_headers).json()
    assert after["waiting_approval"] == []
    replay = s.client.post(
        f"/remote/v1/approvals/{ref}/approve",
        json={"intent_id": "i-approve-replay", "project_id": PROJECT, "target_run_id": run_id,
              "step_up_proof": proof},
        headers=headers,
    )
    assert replay.status_code in {403, 422}
    assert s.gateway.versions[PROJECT] == 1 and len(s.ports.commit_calls) == 1


def test_scope_version_change_blocks_remote_approval(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    run_id = s.human_gated_run()
    ref = s.client.get("/remote/v1/approvals", headers=s.owner_headers).json()["pending"][0]["approval_ref"]
    headers, proof = s.step_up(ref)
    s.gateway.versions[PROJECT] = 7  # another accepted change moved the scope
    decision = s.client.post(
        f"/remote/v1/approvals/{ref}/approve",
        json={"intent_id": "i-approve-2", "project_id": PROJECT, "target_run_id": run_id, "step_up_proof": proof},
        headers=headers,
    ).json()
    assert decision["outcome"] == "SCOPE_VERSION_CHANGED"
    assert s.ports.commit_calls == []
    stale = s.client.post(f"/remote/v1/approvals/{ref}/step-up", headers=headers, json={"action": "APPROVE"})
    assert stale.status_code == 403 and stale.json()["detail"] == "APPROVAL_SCOPE_VERSION_STALE"


def test_viewer_cannot_approve_or_control(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    run_id = s.human_gated_run()
    ref = s.client.get("/remote/v1/approvals", headers=s.viewer_headers).json()["pending"][0]["approval_ref"]
    viewer = s.session_headers(s.viewer_headers)
    challenge = s.client.post(f"/remote/v1/approvals/{ref}/step-up", headers=viewer, json={"action": "APPROVE"})
    assert challenge.status_code == 403 and challenge.json()["detail"] == "REMOTE_APPROVAL_NOT_ALLOWED"
    forged = s.client.post(
        f"/remote/v1/approvals/{ref}/approve",
        json={"intent_id": "i-v", "project_id": PROJECT, "target_run_id": run_id,
              "step_up_proof": "step-up-proof:00000000-0000-4000-8000-000000000009"},
        headers=viewer,
    )
    # A non-approver is refused (and ledgered) before any step-up proof is examined.
    assert forged.status_code == 200 and forged.json()["outcome"] == "ACTOR_NOT_AUTHORIZED"
    cancel = s.client.post(f"/remote/v1/jobs/{run_id}/cancel", json={"intent_id": "i-c", "project_id": PROJECT},
                           headers=s.viewer_headers)
    assert cancel.status_code == 403
    assert s.ports.commit_calls == []


def test_pause_and_cancel_report_unsupported(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    run_id = s.human_gated_run()
    pause = s.client.post(f"/remote/v1/jobs/{run_id}/pause", json={"intent_id": "i-p", "project_id": PROJECT},
                          headers=s.owner_headers).json()
    assert pause["outcome"] in {"PAUSE_UNSUPPORTED", "JOB_ALREADY_TERMINAL"}


def test_unauthenticated_and_wrong_credentials_are_refused(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    assert s.client.get("/remote/v1/attention").status_code == 401
    bad = {**s.owner_headers, "X-PADOS-Remote-Proof": "not-it"}
    assert s.client.get("/remote/v1/attention", headers=bad).status_code == 401


def test_default_credentials_disable_all_remote_routes(tmp_path: Path) -> None:
    s = build_slice(tmp_path, settings=Settings())
    response = s.client.get("/remote/v1/attention", headers=s.owner_headers)
    assert response.status_code == 503
    assert "DEFAULT_CONTROL_CREDENTIAL_FORBIDDEN" in response.json()["detail"]["violations"]


def test_public_bind_disables_remote_routes(tmp_path: Path) -> None:
    s = build_slice(tmp_path, bind_host="203.0.113.9")
    assert s.client.get("/remote/v1/home-base", headers=s.owner_headers).status_code == 503


def test_upload_then_attach_to_intent(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    up = s.client.post(
        "/remote/v1/uploads",
        params={"project_id": PROJECT, "name": "brief.md", "classification": "internal"},
        content=b"# brief\n",
        headers=s.owner_headers,
    )
    assert up.status_code == 200, up.text
    staged = up.json()
    assert staged["state"] == "STAGED" and staged["scan_status"] == "MALWARE_SCAN_NOT_CONFIGURED"
    status = s.client.get(f"/remote/v1/uploads/{staged['staged_id']}", headers=s.owner_headers).json()
    assert status["integrity_verified"] is True
    intent = s.client.post(
        "/remote/v1/intents",
        json={"intent_id": "i-ingest", "project_id": PROJECT, "intent_type": "UPLOAD_AND_INGEST",
              "human_message": "ingest the brief", "attachment_refs": [staged["ref"]]},
        headers=s.owner_headers,
    )
    assert intent.status_code == 200, intent.text
    bad = s.client.post(
        "/remote/v1/uploads",
        params={"project_id": PROJECT, "name": "../escape.md", "classification": "internal"},
        content=b"x", headers=s.owner_headers,
    )
    assert bad.status_code == 422


def test_no_shell_sql_or_model_passthrough_routes(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    paths = {getattr(r, "path", "") for r in s.client.app.routes}  # type: ignore[attr-defined]
    remote = sorted(p for p in paths if p.startswith("/remote"))
    for forbidden in ("shell", "exec", "sql", "query", "ollama", "model", "proxy", "terminal", "files/browse"):
        assert not any(forbidden in p for p in remote), (forbidden, remote)
    home = s.client.get("/remote/v1/home-base", headers=s.owner_headers).json()
    assert home["remote_shell"] == "NOT_PROVIDED"
    assert home["network_model"] == "PRIVATE_OVERLAY_ONLY"


def test_device_enrollment_uses_server_challenge_and_changes_no_identity(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    from tests.remote.presence_support import SoftwareAuthenticator

    device = SoftwareAuthenticator()
    options = s.client.post("/remote/v1/presence/enrollment-options", headers=s.owner_headers).json()["public_key"]
    forged = dict(options, challenge="A" * 43)
    refused = s.client.post("/remote/v1/presence/enrollment-descriptor", headers=s.owner_headers,
                            json={"registration": device.registration(forged), "challenge": "A" * 43})
    assert refused.status_code == 403 and refused.json()["detail"] == "ENROLLMENT_CHALLENGE_INVALID"
    options = s.client.post("/remote/v1/presence/enrollment-options", headers=s.owner_headers).json()["public_key"]
    accepted = s.client.post("/remote/v1/presence/enrollment-descriptor", headers=s.owner_headers,
                             json={"registration": device.registration(options), "challenge": options["challenge"]})
    assert accepted.status_code == 200, accepted.text
    descriptor = accepted.json()["descriptor"]
    assert descriptor["attestation_verified"] is False
    actor = s.service.registry.get("owner")
    assert actor is not None and descriptor["credential_id"] not in {c.credential_id for c in actor.presence_credentials}
