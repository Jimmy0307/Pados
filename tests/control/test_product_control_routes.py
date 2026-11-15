# SPDX-License-Identifier: AGPL-3.0-only
"""The product control routes over HTTP: the control token and a paired session (CSRF + Origin) both work, nothing
else does; refusals carry closed codes; documents are written only inside the project folder; replays by key.

The database is replaced by recorded stand-ins here; tests/integration/test_control_product_postgres.py runs the
same services against PostgreSQL.
"""
from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pados.control.product_errors import ProductControlError
from tests.identity.conftest import make_harness  # noqa: F401 - fixture
from tests.identity.identity_support import ORIGIN, TOKEN_HEADERS, Harness

PROJECT = "11111111-2222-4333-8444-555555555555"
JOB = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def _code(response: Any) -> str:
    return str(response.json()["error"]["code"])


class FakeSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any, _params: Any = None) -> Any:
        self.statements.append(str(statement))
        return None


@pytest.fixture
def world(tmp_path: Path, make_harness: Callable[..., Harness],  # noqa: F811 - fixture
          monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    from pados.api import app as api_app
    from pados.control import product_routes
    from pados.control import service as control_service

    workspace = tmp_path / "workspace"
    folder = workspace / "demo-0a1b2c3d"
    folder.mkdir(parents=True)
    events: list[dict[str, Any]] = []
    session = FakeSession()

    @contextlib.contextmanager
    def fake_scope(_settings: object = None) -> Iterator[FakeSession]:
        yield session

    monkeypatch.setattr(api_app, "session_scope", fake_scope)
    monkeypatch.setattr(product_routes, "project_root_path", lambda _s, pid: str(folder) if pid == PROJECT
                        else (_ for _ in ()).throw(__import__("sqlalchemy.exc").exc.NoResultFound()))
    monkeypatch.setattr(product_routes, "prior_document_write", lambda _s, _p, key: next(
        (e["payload"] for e in events if e["payload"].get("idempotency_key") == key), None))
    monkeypatch.setattr(control_service, "append_sync_event", lambda _s, pid, kind, payload: events.append(
        {"project_id": pid, "event_type": kind, "payload": payload}))
    harness = make_harness(PADOS_PRODUCT_WORKSPACE_ROOT=str(workspace),
                           PADOS_ARTIFACT_ROOT=str(tmp_path / "artifacts"))
    yield {"harness": harness, "folder": folder, "events": events, "session": session, "tmp": tmp_path}


def _session_client(harness: Harness) -> tuple[TestClient, dict[str, str]]:
    client = harness.client()
    csrf = str(harness.paired(client)["csrf_token"])
    return client, {"Origin": ORIGIN, "X-PADOS-CSRF": csrf, "Sec-Fetch-Site": "same-origin"}


# --- POST /control/projects -------------------------------------------------------------------------------------------
@pytest.fixture
def creation(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from pados.control import product_projects, product_routes

    state: dict[str, Any] = {"calls": [], "scopes": [], "fail_scope": False, "refuse": None}

    def create(_session: object, _settings: object, payload: Any) -> dict[str, Any]:
        if state["refuse"]:
            raise ProductControlError(*state["refuse"])
        state["calls"].append(payload.model_dump())
        replayed = len(state["calls"]) > 1 and state["calls"][0] == state["calls"][-1]
        return {"project_id": PROJECT, "replayed": replayed}

    def scope(_settings: object, project_id: str) -> int:
        if state["fail_scope"]:
            raise RuntimeError("proof lane down")
        state["scopes"].append(project_id)
        return 0

    monkeypatch.setattr(product_projects, "create_project", create)
    monkeypatch.setattr(product_projects, "project_view", lambda _s, pid: {"id": pid, "name": "Demo"})
    monkeypatch.setattr(product_routes, "ensure_local_proof_scope", scope)
    return state


BODY = {"name": "Demo", "description": "A demo project", "idempotency_key": "web-project-1"}


def test_create_project_with_the_token_and_replay_by_key(world: dict[str, Any], creation: dict[str, Any]) -> None:
    client = world["harness"].client()
    first = client.post("/control/projects", json=BODY, headers=TOKEN_HEADERS)
    assert first.status_code == 200, first.text
    assert first.json()["project"] == {"id": PROJECT, "name": "Demo"}
    assert first.json()["proof_scope"] == "READY" and first.json()["replayed"] is False
    again = client.post("/control/projects", json=BODY, headers=TOKEN_HEADERS)
    assert again.json()["replayed"] is True
    assert creation["scopes"] == [PROJECT, PROJECT]  # a replay re-runs the idempotent proof-scope setup
    assert creation["calls"][0]["classification"] == "internal"  # the CLI's default


def test_create_project_with_a_session_needs_csrf_and_origin(world: dict[str, Any], creation: dict[str, Any]) -> None:
    client, headers = _session_client(world["harness"])
    assert client.post("/control/projects", json=BODY, headers=headers).status_code == 200
    no_csrf = client.post("/control/projects", json=BODY, headers={"Origin": ORIGIN})
    assert no_csrf.status_code == 403 and _code(no_csrf) == "CSRF_INVALID"
    foreign = client.post("/control/projects", json=BODY, headers={**headers, "Origin": "http://evil.example"})
    assert foreign.status_code == 403 and _code(foreign) == "ORIGIN_NOT_ALLOWED"
    anonymous = world["harness"].client().post("/control/projects", json=BODY)
    assert anonymous.status_code == 403
    assert len(creation["calls"]) == 1


@pytest.mark.parametrize(("refusal", "status"), [
    (("PROJECT_NAME_INVALID", 422), 422), (("PROJECT_NAME_TAKEN", 409), 409),
    (("PROJECT_IDEMPOTENCY_CONTENT_CONFLICT", 409), 409), (("PROJECT_WORKSPACE_ROOT_NOT_CONFIGURED", 503), 503),
    (("PROJECT_FOLDER_EXISTS", 409), 409),
])
def test_create_project_refusals_carry_closed_codes(world: dict[str, Any], creation: dict[str, Any],
                                                   refusal: tuple[str, int], status: int) -> None:
    creation["refuse"] = refusal
    response = world["harness"].client().post("/control/projects", json=BODY, headers=TOKEN_HEADERS)
    assert response.status_code == status and _code(response) == refusal[0]
    assert creation["scopes"] == []


def test_a_proof_scope_failure_is_a_closed_retryable_refusal(world: dict[str, Any], creation: dict[str, Any]) -> None:
    creation["fail_scope"] = True
    response = world["harness"].client().post("/control/projects", json=BODY, headers=TOKEN_HEADERS)
    assert response.status_code == 503 and _code(response) == "PROJECT_PROOF_SCOPE_UNAVAILABLE"
    assert "proof lane down" not in response.text


def test_a_missing_idempotency_key_is_a_validation_failure(world: dict[str, Any], creation: dict[str, Any]) -> None:
    response = world["harness"].client().post("/control/projects", json={"name": "Demo"}, headers=TOKEN_HEADERS)
    assert response.status_code == 422 and creation["calls"] == []


# --- documents --------------------------------------------------------------------------------------------------------
DOC = {"path": "guide.md", "content": "[ok](README.md) and [broken](missing.md)\n", "idempotency_key": "doc-1"}


def test_write_list_and_read_a_document_with_a_session(world: dict[str, Any]) -> None:
    client, headers = _session_client(world["harness"])
    written = client.post(f"/control/projects/{PROJECT}/documents", json=DOC, headers=headers)
    assert written.status_code == 200, written.text
    receipt = written.json()["document"]
    digest = hashlib.sha256(DOC["content"].encode()).hexdigest()
    assert receipt["sha256"] == digest and receipt["replayed"] is False
    assert (world["folder"] / "guide.md").read_bytes() == DOC["content"].encode()
    assert world["events"][-1]["event_type"] == "PROJECT_DOCUMENT_WRITTEN"
    assert world["events"][-1]["payload"]["idempotency_key"] == "doc-1"
    assert any("pg_advisory_xact_lock" in s for s in world["session"].statements)
    listed = client.get(f"/control/projects/{PROJECT}/documents").json()
    assert [d["path"] for d in listed["documents"]] == ["guide.md"] and listed["writable"] is True
    read = client.get(f"/control/projects/{PROJECT}/documents/content", params={"path": "guide.md"}).json()
    assert read["document"]["content"] == DOC["content"]


def test_document_replay_conflict_and_overwrite_protection(world: dict[str, Any]) -> None:
    client = world["harness"].client()
    url = f"/control/projects/{PROJECT}/documents"
    assert client.post(url, json=DOC, headers=TOKEN_HEADERS).status_code == 200
    replay = client.post(url, json=DOC, headers=TOKEN_HEADERS)
    assert replay.status_code == 200 and replay.json()["document"]["replayed"] is True
    assert len(world["events"]) == 1
    conflict = client.post(url, json={**DOC, "content": "other\n"}, headers=TOKEN_HEADERS)
    assert conflict.status_code == 409 and _code(conflict) == "DOCUMENT_IDEMPOTENCY_CONTENT_CONFLICT"
    exists = client.post(url, json={**DOC, "idempotency_key": "doc-2"}, headers=TOKEN_HEADERS)
    assert exists.status_code == 409 and _code(exists) == "DOCUMENT_EXISTS"
    stale = client.post(url, json={**DOC, "content": "fixed\n", "replace": True, "expected_sha256": "0" * 64,
                                   "idempotency_key": "doc-3"}, headers=TOKEN_HEADERS)
    assert stale.status_code == 409 and _code(stale) == "DOCUMENT_CHANGED"
    current = hashlib.sha256(DOC["content"].encode()).hexdigest()
    fixed = client.post(url, json={**DOC, "content": "[ok](README.md)\n", "replace": True,
                                   "expected_sha256": current, "idempotency_key": "doc-4"}, headers=TOKEN_HEADERS)
    assert fixed.status_code == 200 and fixed.json()["document"]["previous_sha256"] == current
    assert (world["folder"] / "guide.md").read_bytes() == b"[ok](README.md)\n"


@pytest.mark.parametrize(("path", "status", "code"), [
    ("../escape.md", 422, "DOCUMENT_PATH_INVALID"), ("..\\escape.md", 422, "DOCUMENT_PATH_INVALID"),
    ("/etc/passwd.md", 422, "DOCUMENT_PATH_INVALID"), ("guide.txt", 422, "DOCUMENT_PATH_INVALID"),
])
def test_document_traversal_is_refused_and_writes_nothing(world: dict[str, Any], path: str, status: int,
                                                          code: str) -> None:
    response = world["harness"].client().post(f"/control/projects/{PROJECT}/documents",
                                              json={**DOC, "path": path}, headers=TOKEN_HEADERS)
    assert response.status_code == status and _code(response) == code
    assert world["events"] == [] and not (world["tmp"] / "escape.md").exists()
    read = world["harness"].client().get(f"/control/projects/{PROJECT}/documents/content", params={"path": path},
                                         headers=TOKEN_HEADERS)
    assert read.status_code == 422


def test_document_size_limit_and_unknown_or_malformed_project(world: dict[str, Any]) -> None:
    client = world["harness"].client()
    big = client.post(f"/control/projects/{PROJECT}/documents",
                      json={**DOC, "content": "x" * (256 * 1024 + 1)}, headers=TOKEN_HEADERS)
    assert big.status_code == 413 and _code(big) == "DOCUMENT_TOO_LARGE"
    malformed = client.post("/control/projects/not-a-uuid/documents", json=DOC, headers=TOKEN_HEADERS)
    assert malformed.status_code == 422 and _code(malformed) == "INVALID_IDENTIFIER"
    unknown = client.get("/control/projects/99999999-2222-4333-8444-555555555555/documents", headers=TOKEN_HEADERS)
    assert unknown.status_code == 404
    assert world["events"] == []


def test_document_routes_refuse_without_a_credential(world: dict[str, Any]) -> None:
    client = world["harness"].client()
    assert client.get(f"/control/projects/{PROJECT}/documents").status_code == 403
    assert client.post(f"/control/projects/{PROJECT}/documents", json=DOC,
                       headers={"Origin": ORIGIN}).status_code == 403
    assert not (world["folder"] / "guide.md").exists()


# --- projections ------------------------------------------------------------------------------------------------------
def test_work_projection_is_read_only_and_reports_step_up_readiness(world: dict[str, Any],
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.control import product_projects, work_projection

    monkeypatch.setattr(product_projects, "project_view", lambda _s, pid: {"id": pid, "name": "Demo"})
    monkeypatch.setattr(work_projection, "project_work", lambda _s, pid: {
        "projection": "pados.control.project-work.v1", "authority": "PROJECTION_ONLY", "loops": []})
    client, _ = _session_client(world["harness"])
    body = client.get(f"/control/projects/{PROJECT}/work").json()
    assert body["authority"] == "PROJECTION_ONLY" and body["project"]["name"] == "Demo"
    assert body["step_up"] == {"remote_console": "NOT_CONFIGURED", "presence_device": "UNKNOWN", "approvers": 0}


def test_artifact_content_route(world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.control import work_projection

    seen: list[tuple[str, str]] = []

    def content(_s: object, root: Path, job_id: str, kind: str) -> dict[str, Any]:
        seen.append((job_id, kind))
        if kind == "handoff_json":
            raise ProductControlError("ARTIFACT_TYPE_NOT_READABLE", 422)
        return {"job_id": job_id, "artifact_type": kind, "content": "{}", "integrity": "VERIFIED"}

    monkeypatch.setattr(work_projection, "artifact_content", content)
    client = world["harness"].client()
    ok = client.get(f"/control/jobs/{JOB}/artifacts/stderr/content", headers=TOKEN_HEADERS)
    assert ok.status_code == 200 and ok.json()["artifact"]["integrity"] == "VERIFIED"
    refused = client.get(f"/control/jobs/{JOB}/artifacts/handoff_json/content", headers=TOKEN_HEADERS)
    assert refused.status_code == 422 and _code(refused) == "ARTIFACT_TYPE_NOT_READABLE"
    assert client.get(f"/control/jobs/{JOB}/artifacts/stderr/content").status_code == 403
    assert client.get("/control/jobs/x/artifacts/stderr/content", headers=TOKEN_HEADERS).status_code == 422
    assert seen == [(JOB, "stderr"), (JOB, "handoff_json")]


def test_work_map_projection_joins_work_and_execution_read_only(world: dict[str, Any],
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.control import product_projects, work_projection

    monkeypatch.setattr(product_projects, "project_view", lambda _s, pid: {"id": pid, "name": "Demo"})
    monkeypatch.setattr(work_projection, "project_work", lambda _s, pid: {
        "projection": "pados.control.project-work.v1", "authority": "PROJECTION_ONLY", "loops": [], "goals": []})
    monkeypatch.setattr(work_projection, "project_execution", lambda _s, pid: {
        "iterations": [{"id": "i1", "job_id": JOB}], "jobs": [{"id": JOB, "status": "SUCCEEDED"}],
        "artifacts": [{"job_id": JOB, "artifact_type": "stderr", "sha256": "a" * 64}], "truncated": False})
    client, _ = _session_client(world["harness"])
    body = client.get(f"/control/projects/{PROJECT}/work-map").json()
    assert body["projection"] == "pados.control.work-map.v1" and body["authority"] == "PROJECTION_ONLY"
    assert body["execution"]["jobs"][0]["status"] == "SUCCEEDED" and body["project"]["name"] == "Demo"
    assert body["step_up"]["remote_console"] == "NOT_CONFIGURED"
    assert world["session"].statements == []  # the stand-ins answered: no statement reached the session
    plain = world["harness"].client()
    assert plain.get(f"/control/projects/{PROJECT}/work-map").status_code == 403
    assert plain.get("/control/projects/not-a-uuid/work-map", headers=TOKEN_HEADERS).status_code == 422


def test_resources_summary_route_is_read_only_and_needs_a_credential(world: dict[str, Any],
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.control import resource_summary

    calls: list[object] = []

    def summary(settings: object, scope: object) -> dict[str, Any]:
        calls.append(settings)
        return {"schema": "pados.control.resources.v1", "authority": "PROJECTION_ONLY", "metrics": [],
                "cleanup_plan": {"deletes": False, "execute": {"available": False}}}

    monkeypatch.setattr(resource_summary, "resources_summary", summary)
    client, _ = _session_client(world["harness"])
    body = client.get("/control/resources").json()
    assert body["status"] == "ok" and body["authority"] == "PROJECTION_ONLY"
    assert body["cleanup_plan"]["execute"]["available"] is False
    token = world["harness"].client().get("/control/resources", headers=TOKEN_HEADERS)
    assert token.status_code == 200
    assert world["harness"].client().get("/control/resources").status_code == 403
    assert len(calls) == 2
