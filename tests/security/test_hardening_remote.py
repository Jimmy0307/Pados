"""Security Hardening V1: adversarial verification of the Remote Operations surface.

Every test attacks an existing control (authentication, project ACL, replay /
duplicate-execution guard, approval binding, upload bounds, deployment guard,
intent-not-context) and asserts it fails closed. Tests marked FIXED in
docs/security/SECURITY_CONTROLS_MATRIX.md failed before the hardening fix.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pados.config import Settings
from pados.product_loop.schemas import GoalRequest
from pados.remote import auth as remote_auth
from pados.remote import routes as remote_routes
from pados.remote.auth import RemoteActor, RemoteActorRegistry
from pados.remote.deployment import check_remote_deployment, classify_bind_address
from pados.remote.intent import FORBIDDEN_CONTEXT_KEYS, RemoteIntentError
from pados.remote.routes import ACTOR_HEADER, PREFIX, PROOF_HEADER
from tests.remote.remote_slice_support import PROJECT, Slice, build_slice, hardened_settings

OTHER_PROJECT = "proj-other"


def _with_outsider(s: Slice) -> dict[str, str]:
    """Add an actor fully privileged on another project only."""
    owner, viewer = s.service.registry.get("owner"), s.service.registry.get("viewer")
    assert owner is not None and viewer is not None
    value = secrets.token_hex(16)
    outsider = RemoteActor(
        actor_id="outsider", credential_hash=hashlib.sha256(value.encode()).hexdigest().upper(),
        projects=frozenset({OTHER_PROJECT}), may_approve=True, may_control_jobs=True,
    )
    s.service.registry = RemoteActorRegistry([owner, viewer, outsider])
    return {ACTOR_HEADER: "outsider", PROOF_HEADER: value}


def _remote_requests(s: Slice) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """One well-formed request per registered /remote/v1 route and method."""
    for route in s.client.app.routes:  # type: ignore[attr-defined]
        path = getattr(route, "path", "")
        if not path.startswith(PREFIX):
            continue
        url = re.sub(r"\{[^}]+\}", "x-1", path)
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            kwargs: dict[str, Any] = {}
            if method == "POST" and path.endswith("/uploads"):
                kwargs = {"params": {"project_id": PROJECT, "name": "a.txt", "classification": "internal"},
                          "content": b"payload"}
            elif method == "POST":
                kwargs = {"json": {}}
            yield method, url, kwargs


def test_every_remote_route_requires_an_authenticated_actor(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    requests = list(_remote_requests(s))
    covered = {url for _, url, _ in requests}
    assert {f"{PREFIX}/intents", f"{PREFIX}/uploads", f"{PREFIX}/attention", f"{PREFIX}/nodes",
            f"{PREFIX}/home-base", f"{PREFIX}/jobs/x-1/cancel", f"{PREFIX}/approvals/x-1/approve"} <= covered
    owner_proof = s.owner_headers[PROOF_HEADER]
    attempts: list[dict[str, str]] = [
        {},
        {ACTOR_HEADER: "owner"},
        {PROOF_HEADER: owner_proof},
        {ACTOR_HEADER: "owner", PROOF_HEADER: "not-the-proof"},
        {ACTOR_HEADER: "ghost", PROOF_HEADER: owner_proof},
        {ACTOR_HEADER: "owner", PROOF_HEADER: hashlib.sha256(owner_proof.encode()).hexdigest()},  # pass-the-hash
        {ACTOR_HEADER: "owner", PROOF_HEADER: ""},
    ]
    for method, url, kwargs in requests:
        for headers in attempts:
            response = s.client.request(method, url, headers=headers, **kwargs)
            assert response.status_code == 401, (method, url, headers.keys(), response.text)
    assert s.ports.work_requests == {} and s.ports.commit_calls == []


def _shipped_defaults(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """The shipped development defaults, independent of any disposable gate credentials."""
    for name in ("PADOS_DB_PASSWORD", "PADOS_CONTROL_API_TOKEN_HASH"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)


def test_failed_deployment_guard_closes_every_route_even_for_valid_actors(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = build_slice(tmp_path, settings=_shipped_defaults(monkeypatch))
    for method, url, kwargs in _remote_requests(s):
        response = s.client.request(method, url, headers=s.owner_headers, **kwargs)
        assert response.status_code == 503, (method, url)
        assert response.json()["detail"]["remote"] == "DISABLED"


def test_unknown_actor_takes_the_same_constant_time_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    value = secrets.token_hex(16)
    registry = RemoteActorRegistry.from_mapping(
        {"actors": [{"actor_id": "owner", "credential_hash": hashlib.sha256(value.encode()).hexdigest()}]})
    calls: list[tuple[str, str]] = []
    real = remote_auth.hmac.compare_digest

    def counting(a: str, b: str) -> bool:
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(remote_auth.hmac, "compare_digest", counting)
    for actor in ("ghost", "owner"):
        with pytest.raises(PermissionError, match="REMOTE_AUTH_INVALID"):
            registry.authenticate(actor, "wrong-proof")
    assert len(calls) == 2  # unknown actors are not short-circuited before the digest compare
    first = registry.authenticate("owner", value)
    second = registry.authenticate("owner", value)
    assert first.session_id != second.session_id  # no reusable bearer session exists to steal or expire


@pytest.mark.parametrize(
    ("row_extra", "top_extra"),
    [
        ({"pass" + "word": "plain-" + "text"}, {}),
        ({"credential": "plaintext-proof"}, {}),
        ({"access_proof": "plaintext-proof"}, {}),
        ({"api_key": "plaintext"}, {}),
        ({}, {"admin_password": "plaintext"}),
        ({}, {"token": "plaintext"}),
    ],
)
def test_actor_registry_refuses_plaintext_credential_fields(
    row_extra: dict[str, str], top_extra: dict[str, str]
) -> None:
    row = {"actor_id": "owner", "credential_hash": hashlib.sha256(b"x").hexdigest(), **row_extra}
    with pytest.raises(ValueError, match="REMOTE_PLAINTEXT_CREDENTIAL_FORBIDDEN"):
        RemoteActorRegistry.from_mapping({"actors": [row], **top_extra})
    with pytest.raises(ValueError, match="REMOTE_ACTOR_INVALID"):
        RemoteActorRegistry.from_mapping({"actors": [{"actor_id": "owner", "credential_hash": "not-a-" + "digest"}]})


def test_actor_cannot_read_decide_or_attach_across_projects(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    outsider = _with_outsider(s)
    run_id = s.human_gated_run()
    ref = s.client.get(f"{PREFIX}/approvals", headers=s.owner_headers).json()["pending"][0]["approval_ref"]
    upload = s.client.post(f"{PREFIX}/uploads", headers=s.owner_headers, content=b"owner data",
                           params={"project_id": PROJECT, "name": "o.md", "classification": "internal"}).json()

    assert s.client.get(f"{PREFIX}/jobs/{run_id}", headers=outsider).status_code == 404
    assert s.client.get(f"{PREFIX}/uploads/{upload['staged_id']}", headers=outsider).status_code == 404
    view = s.client.get(f"{PREFIX}/attention", headers=outsider).json()
    assert all(not rows for key, rows in view.items() if isinstance(rows, list))
    assert s.client.get(f"{PREFIX}/approvals", headers=outsider).json() == {"pending": []}

    forged_proof = "step-up-proof:00000000-0000-4000-8000-0000000000aa"
    claim_victim = s.client.post(f"{PREFIX}/approvals/{ref}/approve", headers=outsider,
                                 json={"intent_id": "x-1", "project_id": PROJECT, "target_run_id": run_id,
                                       "step_up_proof": forged_proof})
    assert claim_victim.status_code == 403 and claim_victim.json()["detail"] == "REMOTE_PROJECT_NOT_ALLOWED"
    claim_own = s.client.post(f"{PREFIX}/approvals/{ref}/approve", headers=outsider,
                              json={"intent_id": "x-2", "project_id": OTHER_PROJECT, "target_run_id": run_id,
                                    "step_up_proof": forged_proof})
    assert claim_own.status_code == 422 and claim_own.json()["detail"] == "APPROVAL_TARGET_MISMATCH"
    query = s.client.post(f"{PREFIX}/intents", headers=outsider, json={
        "intent_id": "x-3", "project_id": OTHER_PROJECT, "intent_type": "QUERY_STATUS", "target_run_id": run_id})
    assert query.status_code == 422 and query.json()["detail"] == "TARGET_RUN_NOT_FOUND"
    cancel = s.client.post(f"{PREFIX}/jobs/{run_id}/cancel", headers=outsider,
                           json={"intent_id": "x-4", "project_id": OTHER_PROJECT})
    assert cancel.status_code == 422 and cancel.json()["detail"] == "TARGET_RUN_NOT_FOUND"
    attach = s.client.post(f"{PREFIX}/intents", headers=outsider, json={
        "intent_id": "x-5", "project_id": OTHER_PROJECT, "intent_type": "UPLOAD_AND_INGEST",
        "human_message": "ingest", "attachment_refs": [upload["ref"]]})
    assert attach.status_code == 422 and attach.json()["detail"] == "ATTACHMENT_PROJECT_MISMATCH"
    assert s.ports.commit_calls == [] and s.gateway.versions.get(PROJECT, 0) == 0


def test_viewer_cannot_approve_reject_or_control(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    run_id = s.human_gated_run()
    ref = s.client.get(f"{PREFIX}/approvals", headers=s.viewer_headers).json()["pending"][0]["approval_ref"]
    for action in ("approve", "reject"):
        outcome = s.client.post(f"{PREFIX}/approvals/{ref}/{action}", headers=s.viewer_headers,
                                json={"intent_id": f"v-{action}", "project_id": PROJECT, "target_run_id": run_id,
                                      "step_up_proof": "step-up-proof:00000000-0000-4000-8000-0000000000bb"})
        assert outcome.json()["outcome"] == "ACTOR_NOT_AUTHORIZED"
        challenge = s.client.post(f"{PREFIX}/approvals/{ref}/step-up", headers=s.session_headers(s.viewer_headers),
                                  json={"action": action.upper()})
        assert challenge.status_code == 403 and challenge.json()["detail"] == "REMOTE_APPROVAL_NOT_ALLOWED"
    for action in ("pause", "resume", "cancel"):
        control = s.client.post(f"{PREFIX}/jobs/{run_id}/{action}", headers=s.viewer_headers,
                                json={"intent_id": f"v-{action}", "project_id": PROJECT})
        assert control.status_code == 403 and control.json()["detail"] == "REMOTE_JOB_CONTROL_NOT_ALLOWED"
    assert s.ports.commit_calls == [] and s.ports.approvals[ref]["status"] == "PENDING"


def test_reconnect_replay_returns_the_original_outcome_without_redispatch(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    body = {"intent_id": "i-reconnect", "project_id": PROJECT, "intent_type": "START_TASK",
            "human_message": "update the README"}
    first = s.client.post(f"{PREFIX}/intents", json=body, headers=s.owner_headers).json()
    for _ in range(3):  # each request is a new authenticated session (reconnect)
        again = s.client.post(f"{PREFIX}/intents", json=body, headers=s.owner_headers).json()
        assert again["replayed"] is True and again["run_id"] == first["run_id"]
        assert again["ledger_entry_hash"] == first["ledger_entry_hash"]
    assert len(s.ports.work_requests) == 1
    assert s.service.ledger.verify()


@pytest.mark.parametrize("who", ["owner_changed_body", "viewer_same_body"])
def test_intent_id_reuse_by_another_body_or_actor_fails_closed(tmp_path: Path, who: str) -> None:
    s = build_slice(tmp_path)
    body = {"intent_id": "i-shared", "project_id": PROJECT, "intent_type": "START_TASK", "human_message": "a"}
    assert s.client.post(f"{PREFIX}/intents", json=body, headers=s.owner_headers).status_code == 200
    if who == "owner_changed_body":
        response = s.client.post(f"{PREFIX}/intents", headers=s.owner_headers, json={**body, "human_message": "b"})
    else:
        response = s.client.post(f"{PREFIX}/intents", headers=s.viewer_headers, json=body)
    assert response.status_code == 422 and response.json()["detail"] == "INTENT_ID_REUSED"
    assert "run_id" not in response.text  # another actor never receives the original outcome
    assert len(s.ports.work_requests) == 1


def test_approval_replay_and_reuse_never_double_commit(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    run_id = s.human_gated_run()
    ref = s.client.get(f"{PREFIX}/approvals", headers=s.owner_headers).json()["pending"][0]["approval_ref"]
    headers, proof = s.step_up(ref)
    body = {"intent_id": "i-approve-once", "project_id": PROJECT, "target_run_id": run_id, "step_up_proof": proof}
    first = s.client.post(f"{PREFIX}/approvals/{ref}/approve", json=body, headers=headers).json()
    assert first["outcome"] == "COMMITTED"
    replay = s.client.post(f"{PREFIX}/approvals/{ref}/approve", json=body, headers=headers).json()
    assert replay["replayed"] is True and replay["binding_hash"] == first["binding_hash"]
    fresh = s.client.post(f"{PREFIX}/approvals/{ref}/approve", headers=headers,
                          json={**body, "intent_id": "i-approve-twice"})
    assert fresh.status_code == 422 and fresh.json()["detail"] == "APPROVAL_REF_NOT_FOUND"
    assert len(s.ports.commit_calls) == 1 and s.gateway.versions[PROJECT] == 1


def test_approval_is_bound_to_the_presented_run(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    first_run = s.human_gated_run()
    second_run = s.human_gated_run("def result():\n    return 'good'  # second\n")
    pending = s.client.get(f"{PREFIX}/approvals", headers=s.owner_headers).json()["pending"]
    ref = next(row["approval_ref"] for row in pending if row["run_id"] == first_run)
    headers, proof = s.step_up(ref)
    swapped = s.client.post(f"{PREFIX}/approvals/{ref}/approve", headers=headers,
                            json={"intent_id": "i-swap", "project_id": PROJECT, "target_run_id": second_run,
                                  "step_up_proof": proof})
    assert swapped.status_code == 422 and swapped.json()["detail"] == "APPROVAL_TARGET_MISMATCH"
    forged = s.client.post(f"{PREFIX}/approvals/{'0' * 8}-forged/approve", headers=headers,
                           json={"intent_id": "i-forged", "project_id": PROJECT, "target_run_id": first_run,
                                 "step_up_proof": proof})
    assert forged.status_code == 422 and forged.json()["detail"] == "APPROVAL_REF_NOT_FOUND"
    assert s.ports.commit_calls == []


def test_concurrent_duplicate_intent_is_dispatched_once(tmp_path: Path) -> None:
    """FIXED: two in-flight requests with one intent_id must not both reach the authority port."""
    s = build_slice(tmp_path)
    original = s.ports.submit_task
    dispatched: list[str] = []
    gate = asyncio.Event()

    async def slow_submit(envelope: Any, context: Any) -> str:
        dispatched.append(envelope.intent_id)
        await gate.wait()  # an authority port that genuinely awaits (DB, queue, network)
        return await original(envelope, context)

    s.ports.submit_task = slow_submit  # type: ignore[method-assign]
    body = {"intent_id": "i-race", "project_id": PROJECT, "intent_type": "START_TASK", "human_message": "go"}
    proof = s.owner_headers[PROOF_HEADER]

    async def scenario() -> list[Any]:
        first = asyncio.create_task(s.service.submit(s.service.registry.authenticate("owner", proof), body))
        for _ in range(3):
            await asyncio.sleep(0)
        second = asyncio.create_task(s.service.submit(s.service.registry.authenticate("owner", proof), body))
        for _ in range(3):
            await asyncio.sleep(0)
        gate.set()
        return list(await asyncio.gather(first, second, return_exceptions=True))

    first, second = asyncio.run(scenario())
    assert dispatched == ["i-race"]
    assert isinstance(second, RemoteIntentError) and second.code == "INTENT_IN_FLIGHT"
    assert first["outcome"] == "SUBMITTED"
    retry = asyncio.run(s.service.submit(s.service.registry.authenticate("owner", proof), body))
    assert retry["replayed"] is True and retry["run_id"] == first["run_id"]
    assert dispatched == ["i-race"]


def test_chunked_upload_cannot_bypass_the_body_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """FIXED: a body without Content-Length was buffered in full before any size check."""
    monkeypatch.setattr(remote_routes, "MAX_UPLOAD_BODY", 1024)
    s = build_slice(tmp_path)
    params = {"project_id": PROJECT, "name": "big.txt", "classification": "internal"}

    def chunks() -> Iterator[bytes]:
        for _ in range(4):
            yield b"A" * 1024

    streamed = s.client.post(f"{PREFIX}/uploads", params=params, content=chunks(), headers=s.owner_headers)
    assert streamed.status_code == 413 and streamed.json()["detail"] == "UPLOAD_TOO_LARGE"
    declared = s.client.post(f"{PREFIX}/uploads", params=params, content=b"A" * 2048, headers=s.owner_headers)
    assert declared.status_code == 413
    small = s.client.post(f"{PREFIX}/uploads", params=params, content=iter([b"ok"]), headers=s.owner_headers)
    assert small.status_code == 200 and small.json()["size_bytes"] == 2
    assert len(list((tmp_path / "staging").iterdir())) == 1


@pytest.mark.parametrize("key", sorted(FORBIDDEN_CONTEXT_KEYS | {"actor_id", "session_id"}))
def test_client_cannot_ship_context_history_or_identity(tmp_path: Path, key: str) -> None:
    s = build_slice(tmp_path)
    body = {"intent_id": "i-ctx", "project_id": PROJECT, "intent_type": "START_TASK", "human_message": "x",
            key: "client supplied"}
    response = s.client.post(f"{PREFIX}/intents", json=body, headers=s.owner_headers)
    assert response.status_code == 422
    assert response.json()["detail"] in {"CLIENT_CONTEXT_FORBIDDEN", "CLIENT_IDENTITY_FORBIDDEN"}
    assert s.ports.work_requests == {}


@pytest.mark.parametrize("ref", ["staged:../../etc/passwd", "file:///etc/passwd", "stg-0123", "staged:"])
def test_attachments_must_be_staged_references(tmp_path: Path, ref: str) -> None:
    s = build_slice(tmp_path)
    response = s.client.post(f"{PREFIX}/intents", headers=s.owner_headers, json={
        "intent_id": "i-att", "project_id": PROJECT, "intent_type": "UPLOAD_AND_INGEST",
        "human_message": "ingest", "attachment_refs": [ref]})
    assert response.status_code == 422 and response.json()["detail"] == "ATTACHMENT_REF_NOT_STAGED"


def test_no_shell_terminal_sql_or_model_passthrough_route_exists(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    segments = {segment for _, url, _ in _remote_requests(s) for segment in url.split("/")}
    forbidden = {"shell", "exec", "execute", "terminal", "pty", "ssh", "sql", "fs", "browse", "proxy",
                 "completions", "chat", "ollama", "models", "command"}
    assert not segments & forbidden


def _goal(s: Slice, *, content: str, human_gate: bool) -> GoalRequest:
    return GoalRequest.model_validate({
        "goal": "Provider claims success.", "workspace_root": str(s.workspace), "scope_id": PROJECT,
        "changes": [{"path": "app.py", "content": content}],
        "acceptance_criteria": [
            {"criterion_id": "good", "check": "FILE_CONTAINS", "target_path": "app.py",
             "expected_text": "return 'good'"},
            {"criterion_id": "scoped", "check": "CHANGED_PATHS_SCOPED"}],
        "authorized": True, "human_gate": human_gate,
    })


@pytest.mark.parametrize("human_gate", [False, True])
def test_model_output_cannot_reach_semantic_state_without_validation(tmp_path: Path, human_gate: bool) -> None:
    s = build_slice(tmp_path)
    report = asyncio.run(s.loop.execute(_goal(s, content="def result():\n    return 'bad'\n",
                                              human_gate=human_gate)))
    assert s.gateway.versions.get(PROJECT, 0) == 0
    assert getattr(report, "commit", None) is None
    pending = s.client.get(f"{PREFIX}/approvals", headers=s.owner_headers).json()["pending"]
    assert pending == []  # a failed candidate is never presented for human decision
    assert s.ports.commit_calls == []


@pytest.mark.parametrize(
    ("host", "violation"),
    [
        ("0.0.0.0", "WILDCARD_BIND_FORBIDDEN"),
        ("::", "WILDCARD_BIND_FORBIDDEN"),
        ("8.8.8.8", "DIRECT_PUBLIC_INTERNET_UNSUPPORTED"),
        ("192.0.2.10", "DIRECT_PUBLIC_INTERNET_UNSUPPORTED"),  # documentation range (is_private in CPython)
        ("2002:808:808::1", "DIRECT_PUBLIC_INTERNET_UNSUPPORTED"),  # 6to4 embedding a public IPv4
        ("::ffff:10.0.0.5", "DIRECT_PUBLIC_INTERNET_UNSUPPORTED"),  # mapped forms fail closed
        ("home-base.example", "BIND_ADDRESS_MUST_BE_LITERAL_IP"),
    ],
)
def test_deployment_guard_refuses_non_overlay_binds(host: str, violation: str) -> None:
    check = check_remote_deployment(hardened_settings(), remote_bind_host=host)
    assert not check.ok and violation in check.violations


def test_deployment_guard_refuses_default_credentials_and_exposed_database(monkeypatch: pytest.MonkeyPatch) -> None:
    defaults = check_remote_deployment(_shipped_defaults(monkeypatch), remote_bind_host="100.64.0.10")
    assert {"DEFAULT_CONTROL_CREDENTIAL_FORBIDDEN", "DEFAULT_DATABASE_CREDENTIAL_FORBIDDEN"} <= set(
        defaults.violations)
    exposed = hardened_settings().model_copy(update={"db_host": "10.0.0.5"})
    assert "DATABASE_MUST_NOT_BE_REMOTELY_EXPOSED" in check_remote_deployment(
        exposed, remote_bind_host="100.64.0.10").violations
    assert check_remote_deployment(hardened_settings(), remote_bind_host="100.64.0.10").ok
    assert classify_bind_address("100.64.0.10") == "PRIVATE_OVERLAY"


def test_status_reads_do_not_copy_projection_rows_into_the_intent_ledger(tmp_path: Path) -> None:
    """FIXED: every QUERY_STATUS ledgered the compact rows (goal text) of up to 50 runs."""
    s = build_slice(tmp_path)
    run_id = s.human_gated_run()
    response = s.client.post(f"{PREFIX}/intents", headers=s.owner_headers, json={
        "intent_id": "i-status", "project_id": PROJECT, "intent_type": "QUERY_STATUS"}).json()
    assert [row["run_id"] for row in response["runs"]] == [run_id]  # the client still gets the rows
    ledger = s.service.ledger.path.read_text(encoding="utf-8")
    assert "Remote-requested bounded change" not in ledger and run_id in ledger
    assert s.service.ledger.verify()


def test_ledgered_context_is_the_fingerprint_summary_only(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    s.client.post(f"{PREFIX}/intents", headers=s.owner_headers, json={
        "intent_id": "i-ctx-summary", "project_id": PROJECT, "intent_type": "START_TASK", "human_message": "go"})
    ledger = s.service.ledger.path.read_text(encoding="utf-8")
    assert "Remote slice fixture project" not in ledger  # workspace README text never ledgered
    entry = [line for line in ledger.splitlines() if line.strip()][-1]
    assert set(json.loads(entry)["record"]["context"]) == {
        "envelope_fingerprint", "context_fingerprint", "scope_id", "scope_version", "source_count",
        "context_source", "client_history_included", "provider_history_included"}


def test_ledger_context_summary_schema_matches_reconstructed_summary() -> None:
    import dataclasses

    from pados.remote.context import CONTEXT_SUMMARY_KEYS, ReconstructedContext

    names = {f.name for f in dataclasses.fields(ReconstructedContext)}
    assert "context" in names  # summary() must keep projecting only the fixed fingerprint fields
    source = __import__("inspect").getsource(ReconstructedContext.summary)
    assert {key for key in CONTEXT_SUMMARY_KEYS if f'"{key}"' in source} == CONTEXT_SUMMARY_KEYS


@pytest.mark.parametrize(("server", "expected"), [
    (("0.0.0.0", 8089), "REMOTE_LISTENER_NOT_PRIVATE"),
    (("::", 8089), "REMOTE_LISTENER_NOT_PRIVATE"),
    (("8.8.8.8", 8089), "REMOTE_LISTENER_NOT_PRIVATE"),
    (("192.168.1.50", 8089), "REMOTE_LISTENER_NOT_CONFIGURED_ADDRESS"),
    (("100.64.1.2", 8089), "REMOTE_LISTENER_NOT_ATTESTED"),  # CR-05: configured address alone is not enough
    (("127.0.0.1", 8089), None),
    (None, None),
    (("testserver", 80), None),
])
def test_f05_listener_socket_is_checked_not_only_the_configuration(server: object, expected: str | None) -> None:
    from pados.remote.deployment import listener_violation

    assert listener_violation(server, bind_host="100.64.1.2") == expected


def test_f05_wildcard_listener_fails_closed_on_every_remote_route() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pados.remote.deployment import NetworkModel, RemoteDeploymentCheck
    from pados.remote.routes import register_remote_routes

    app = FastAPI()
    register_remote_routes(app, Settings(_env_file=None), service=None,
                           deployment=RemoteDeploymentCheck(True, NetworkModel.PRIVATE_OVERLAY_ONLY, (), "100.64.1.2"),
                           nodes=lambda: {})

    async def wildcard(scope, receive, send):  # type: ignore[no-untyped-def]
        scope = {**scope, "server": ("0.0.0.0", 8089)}
        await app(scope, receive, send)

    response = TestClient(wildcard).get("/remote/v1/home-base")
    assert response.status_code == 503
    assert response.json()["detail"]["violations"] == ["REMOTE_LISTENER_NOT_PRIVATE"]


def test_f15_two_ledger_instances_in_separate_processes_do_not_fork_the_chain(tmp_path: Path) -> None:
    import multiprocessing

    from pados.remote.ledger import IntentLedger

    path = tmp_path / "ledger" / "intents.jsonl"
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_append_many, args=(str(path), n)) for n in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
        assert worker.exitcode == 0
    assert len(path.read_text(encoding="utf-8").splitlines()) == 4 * 25
    assert IntentLedger(path).verify()


def _append_many(path: str, worker: int) -> None:
    from pados.remote.ledger import IntentLedger

    ledger = IntentLedger(Path(path))
    for index in range(25):
        ledger.append({"intent_id": f"w{worker}-{index}", "outcome": "READ"})
