"""Security Hardening fixer round 1: regression tests for review CR-05 (ten findings, all accepted)."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import multiprocessing
import os
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pados.compute_node.config import BackendConfig, private_http_url
from pados.compute_node.registry import NodeProber, NodeRegistration, NodeRegistry
from pados.enterprise import observability
from pados.enterprise.classification import DataClassification
from pados.enterprise.secrets import EnvironmentSecretResolver
from pados.remote import deployment as remote_deployment
from pados.remote import routes as remote_routes
from pados.remote import serve as remote_serve
from pados.remote import staging as staging_module
from pados.remote.auth import RemoteActorRegistry
from pados.remote.intent import RemoteIntentError
from pados.remote.ledger import IntentLedger, LedgerPathError
from pados.remote.routes import PREFIX, PROOF_HEADER
from pados.remote.staging import BulkInbox, StagingArea, StagingError
from pados.sandbox.adapter import SimulatedSandboxAdapter
from pados.sandbox.policy import DataEgressPolicy, SandboxRequest
from tests.compute_node.node_support import CREDENTIAL_VALUE, ENDPOINT, Node, json_response, make_node, resolver
from tests.remote.remote_slice_support import PROJECT, Slice, build_slice, hardened_settings

OVERLAY = "100.64.0.10"
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file-descriptor semantics")


@pytest.fixture(autouse=True)
def no_listener_attestation() -> Iterator[None]:
    remote_deployment._reset_listener_attestation()
    yield
    remote_deployment._reset_listener_attestation()


def _on_local_address(s: Slice, server: tuple[str, int]) -> TestClient:
    """A client whose requests arrive on ``server`` as the connection's local address."""
    app = s.client.app

    async def asgi(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await app({**scope, "server": server}, receive, send)  # type: ignore[operator]

    return TestClient(asgi)


# -- F-05: listener attestation ------------------------------------------------------------------

def test_cr05_plain_uvicorn_launch_never_serves_the_overlay(tmp_path: Path) -> None:
    """`uvicorn --factory ... --host 0.0.0.0` reports the concrete interface in scope["server"]."""
    s = build_slice(tmp_path, bind_host=OVERLAY)
    overlay = _on_local_address(s, (OVERLAY, 8089))
    for path in ("/home-base", "/attention", "/nodes"):
        response = overlay.get(f"{PREFIX}{path}", headers=s.owner_headers)
        assert response.status_code == 503
        assert response.json()["detail"]["violations"] == ["REMOTE_LISTENER_NOT_ATTESTED"]
    other = _on_local_address(s, ("192.168.1.9", 8089)).get(f"{PREFIX}/home-base", headers=s.owner_headers)
    assert other.json()["detail"]["violations"] == ["REMOTE_LISTENER_NOT_CONFIGURED_ADDRESS"]
    loopback = _on_local_address(s, ("127.0.0.1", 8089)).get(f"{PREFIX}/home-base", headers=s.owner_headers)
    assert loopback.status_code == 200  # loopback (accepted UATs) and in-process clients are unchanged
    assert s.client.get(f"{PREFIX}/home-base", headers=s.owner_headers).status_code == 200


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "8.8.8.8", "192.0.2.10", "localhost", "home-base.example"])
def test_cr05_launcher_refuses_wildcard_public_or_non_literal_binds(host: str) -> None:
    with pytest.raises(remote_serve.LaunchRefused, match="REMOTE_BIND_REFUSED"):
        remote_serve.bind_listener(host, 0)


def _remote_config(tmp_path: Path, bind_host: str) -> Path:
    config = tmp_path / "remote.json"
    config.write_text(json.dumps({
        "bind_host": bind_host, "staging_root": str(tmp_path / "rs"), "ledger_path": str(tmp_path / "rl" / "i.jsonl"),
        "actors": [{"actor_id": "owner", "credential_hash": hashlib.sha256(b"proof").hexdigest()}]}), encoding="utf-8")
    return config


def _settings_with(monkeypatch: pytest.MonkeyPatch, config: Path) -> None:
    import pados.config as pados_config

    settings = hardened_settings().model_copy(update={"remote_config": str(config)})
    monkeypatch.setattr(pados_config, "get_settings", lambda: settings)


def test_cr05_launcher_refuses_a_failing_deployment_guard_before_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings_with(monkeypatch, _remote_config(tmp_path, "0.0.0.0"))
    monkeypatch.setattr(remote_serve, "bind_listener", lambda *_a: pytest.fail("bound despite the guard"))
    with pytest.raises(remote_serve.LaunchRefused, match="WILDCARD_BIND_FORBIDDEN"):
        remote_serve.serve(port=0, app_factory=lambda: None, server_factory=lambda _app: None)
    assert remote_deployment.listener_attestation() is None


class _FakeSocket:
    def __init__(self, name: tuple[str, int]) -> None:
        self.name = name
        self.closed = False

    def getsockname(self) -> tuple[str, int]:
        return self.name

    def close(self) -> None:
        self.closed = True


def test_cr05_attested_launcher_serves_only_its_verified_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = build_slice(tmp_path, bind_host=OVERLAY)
    _settings_with(monkeypatch, _remote_config(tmp_path, OVERLAY))
    sock = _FakeSocket((OVERLAY, 8089))
    monkeypatch.setattr(remote_serve, "bind_listener", lambda host, port: sock if host == OVERLAY else None)
    served: list[Any] = []

    class Server:
        def run(self, sockets: list[Any]) -> None:
            served.extend(sockets)

    assert remote_serve.serve(port=8089, app_factory=lambda: s.client.app, server_factory=lambda _app: Server()) == 0
    assert served == [sock] and sock.closed
    assert remote_deployment.listener_attestation() == remote_deployment.ListenerAttestation(OVERLAY, 8089)
    assert _on_local_address(s, (OVERLAY, 8089)).get(f"{PREFIX}/home-base", headers=s.owner_headers).status_code == 200
    wrong_port = _on_local_address(s, (OVERLAY, 9999)).get(f"{PREFIX}/home-base", headers=s.owner_headers)
    assert wrong_port.json()["detail"]["violations"] == ["REMOTE_LISTENER_NOT_CONFIGURED_ADDRESS"]


def test_cr05_attestation_requires_the_socket_to_be_bound_to_exactly_the_bind_host() -> None:
    for name in (("0.0.0.0", 8089), ("100.64.0.11", 8089), ("not-an-ip", 1)):
        with pytest.raises(PermissionError):
            remote_deployment.attest_listener(_FakeSocket(name), bind_host=OVERLAY)  # type: ignore[arg-type]
    with pytest.raises(PermissionError, match="BIND_REFUSED"):
        remote_deployment.attest_listener(_FakeSocket(("0.0.0.0", 1)), bind_host="0.0.0.0")  # type: ignore[arg-type]
    assert remote_deployment.listener_attestation() is None


def test_cr05_real_launcher_socket_end_to_end_on_loopback(tmp_path: Path) -> None:
    import uvicorn

    s = build_slice(tmp_path, bind_host="127.0.0.1")
    sock = remote_serve.bind_listener("127.0.0.1", 0)
    remote_deployment.attest_listener(sock, bind_host="127.0.0.1")
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(s.client.app, workers=1, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        response = httpx.get(f"http://127.0.0.1:{port}{PREFIX}/home-base", headers=s.owner_headers, timeout=5)
        assert response.status_code == 200 and response.json()["remote_shell"] == "NOT_PROVIDED"
        assert httpx.get(f"http://127.0.0.1:{port}{PREFIX}/home-base", timeout=5).status_code == 401
    finally:
        server.should_exit = True
        thread.join(10)
        sock.close()


# -- INTENT-DUPLICATE: persisted claims ----------------------------------------------------------

def _second_instance(s: Slice, **ledger_options: Any) -> Any:
    """Another service instance (another process in production) on the same ledger file."""
    return dataclasses.replace(s.service, ledger=IntentLedger(s.service.ledger.path, **ledger_options))


def test_cr05_two_instances_sharing_a_ledger_dispatch_an_intent_once(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    other = _second_instance(s)
    original = s.ports.submit_task
    dispatched: list[str] = []
    gate = asyncio.Event()

    async def slow_submit(envelope: Any, context: Any) -> str:
        dispatched.append(envelope.intent_id)
        await gate.wait()
        return await original(envelope, context)

    s.ports.submit_task = slow_submit  # type: ignore[method-assign]
    body = {"intent_id": "i-x", "project_id": PROJECT, "intent_type": "START_TASK", "human_message": "go"}
    proof = s.owner_headers[PROOF_HEADER]

    def session(svc: Any) -> Any:
        return svc.registry.authenticate("owner", proof)

    async def scenario() -> tuple[Any, Any, Any]:
        first = asyncio.create_task(s.service.submit(session(s.service), body))
        for _ in range(5):
            await asyncio.sleep(0)
        assert dispatched == ["i-x"]
        with pytest.raises(RemoteIntentError) as in_flight:
            await other.submit(session(other), body)
        with pytest.raises(RemoteIntentError) as reused:
            await other.submit(session(other), {**body, "human_message": "different"})
        gate.set()
        return await first, in_flight.value.code, reused.value.code

    first, in_flight, reused = asyncio.run(scenario())
    assert (in_flight, reused) == ("INTENT_IN_FLIGHT", "INTENT_ID_REUSED")
    replay = asyncio.run(other.submit(session(other), body))
    assert replay["replayed"] is True and replay["run_id"] == first["run_id"]
    assert replay["ledger_entry_hash"] == first["ledger_entry_hash"]  # find() returns the outcome, not the claim
    assert dispatched == ["i-x"] and len(s.ports.work_requests) == 1
    assert s.service.ledger.verify()


def _claim(path: str, results: Any) -> None:
    ledger = IntentLedger(Path(path))
    results.put(ledger.claim(intent_id="i-race", actor_id="owner", request_fingerprint="f" * 64).status)


def test_cr05_claim_is_atomic_across_processes(tmp_path: Path) -> None:
    path = tmp_path / "ledger" / "intents.jsonl"
    IntentLedger(path)
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    workers = [context.Process(target=_claim, args=(str(path), results)) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
        assert worker.exitcode == 0
    statuses = sorted(results.get(timeout=5) for _ in workers)
    assert statuses == ["CLAIMED", "IN_FLIGHT", "IN_FLIGHT", "IN_FLIGHT"]
    assert IntentLedger(path).verify()


def test_cr05_claim_left_by_a_crash_is_unresolved_and_never_redispatched(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    body = {"intent_id": "i-crash", "project_id": PROJECT, "intent_type": "START_TASK", "human_message": "go"}
    session = s.service.registry.authenticate("owner", s.owner_headers[PROOF_HEADER])
    envelope_fp = __import__("pados.remote.intent", fromlist=["parse_client_intent"]).parse_client_intent(
        body, actor_id="owner", session_id="x").request_fingerprint
    assert s.service.ledger.claim(intent_id="i-crash", actor_id="owner", request_fingerprint=envelope_fp).status == "CLAIMED"
    with pytest.raises(RemoteIntentError, match="INTENT_IN_FLIGHT"):  # within the lease
        asyncio.run(s.service.submit(session, body))
    expired = _second_instance(s, claim_lease_seconds=-1.0)
    with pytest.raises(RemoteIntentError, match="INTENT_CLAIM_UNRESOLVED"):
        asyncio.run(expired.submit(session, body))
    assert s.ports.work_requests == {} and s.service.ledger.find("i-crash") is None


def test_cr05_port_failure_after_dispatch_keeps_the_claim(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    calls: list[str] = []

    async def failing(envelope: Any, context: Any) -> str:
        calls.append(envelope.intent_id)
        raise ConnectionError("database went away after the insert")

    s.ports.submit_task = failing  # type: ignore[method-assign]
    body = {"intent_id": "i-fail", "project_id": PROJECT, "intent_type": "START_TASK", "human_message": "go"}
    session = s.service.registry.authenticate("owner", s.owner_headers[PROOF_HEADER])
    with pytest.raises(ConnectionError):
        asyncio.run(s.service.submit(session, body))
    with pytest.raises(RemoteIntentError, match="INTENT_IN_FLIGHT|INTENT_CLAIM_UNRESOLVED"):
        asyncio.run(s.service.submit(session, body))
    assert calls == ["i-fail"]


def test_cr05_refusal_before_dispatch_releases_the_claim(tmp_path: Path) -> None:
    s = build_slice(tmp_path)
    body = {"intent_id": "i-att", "project_id": PROJECT, "intent_type": "UPLOAD_AND_INGEST", "human_message": "x",
            "attachment_refs": ["staged:stg-00000000000000000000"]}
    session = s.service.registry.authenticate("owner", s.owner_headers[PROOF_HEADER])
    for _ in range(2):  # the same request is re-validated, not reported as in flight
        with pytest.raises(RemoteIntentError, match="ATTACHMENT_NOT_FOUND"):
            asyncio.run(s.service.submit(session, body))
    assert s.service.ledger.verify()


# -- INBOX-OPEN ----------------------------------------------------------------------------------

def _inbox(tmp_path: Path) -> tuple[Path, BulkInbox]:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    area = StagingArea(tmp_path / "staging", project_ceiling=lambda _p: DataClassification.CONFIDENTIAL)
    return inbox, BulkInbox(inbox, area)


@POSIX_ONLY
def test_cr05_fifo_swapped_in_after_the_check_cannot_block_the_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inbox, _ = _inbox(tmp_path)
    (inbox / "drop.csv").write_bytes(b"a,b\n")
    base = type(Path())

    class SwapToFifo(base):  # type: ignore[valid-type, misc]
        def resolve(self, strict: bool = False) -> Any:
            resolved = super().resolve(strict)
            if self.name == "drop.csv" and self.is_file():
                self.unlink()
                os.mkfifo(self)
            return resolved

    monkeypatch.setattr(staging_module, "Path", SwapToFifo)
    bulk = BulkInbox(inbox, StagingArea(tmp_path / "staging", project_ceiling=lambda _p: DataClassification.PUBLIC))
    outcome: list[BaseException] = []

    def attempt() -> None:
        try:
            bulk.register(project_id="p", actor_id="sftp", name="drop.csv", classification="public")
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
            outcome.append(exc)

    worker = threading.Thread(target=attempt, daemon=True)
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "open() blocked on a FIFO"
    assert isinstance(outcome[0], StagingError) and outcome[0].code in {"INBOX_FILE_NOT_REGULAR", "INBOX_FILE_CHANGED"}


def test_cr05_inbox_file_modified_while_copied_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inbox, bulk = _inbox(tmp_path)
    target = inbox / "data.csv"
    target.write_bytes(b"x" * 4096)
    real_read = os.read
    writes: list[int] = []

    def growing_read(fd: int, n: int) -> bytes:
        chunk = real_read(fd, n)
        if chunk and not writes:
            writes.append(1)
            with open(target, "ab") as handle:  # the transport is still writing the same inode
                handle.write(b"appended")
        return chunk

    monkeypatch.setattr(staging_module.os, "read", growing_read)
    with pytest.raises(StagingError, match="INBOX_FILE_CHANGED"):
        bulk.register(project_id="p", actor_id="sftp", name="data.csv", classification="internal")
    monkeypatch.undo()
    assert not list((tmp_path / "staging").glob(".inbox-*")) and not list((tmp_path / "staging").glob("stg-*"))


def test_cr05_inbox_registers_the_snapshot_and_cleans_it_up(tmp_path: Path) -> None:
    inbox, bulk = _inbox(tmp_path)
    (inbox / "ok.csv").write_bytes(b"1,2\n")
    staged = bulk.register(project_id="p", actor_id="sftp", name="ok.csv", classification="internal")
    assert staged.sha256 == hashlib.sha256(b"1,2\n").hexdigest()
    assert not list((tmp_path / "staging").glob(".inbox-*"))


# -- SANDBOX-WINDOWS -----------------------------------------------------------------------------

def _echo_to(names: list[str]) -> Any:
    return lambda inputs: {name: b"x" for name in names}


@pytest.mark.parametrize("name", ["CON ", "con .txt", "report.", "report ", "NUL.tar.gz", "Com1.log", "lpt9",
                                  "aux", "Prn.", "COM0.txt"])
def test_cr05_windows_normalized_device_and_trailing_names_are_refused(tmp_path: Path, name: str) -> None:
    adapter = SimulatedSandboxAdapter(_echo_to(["out.txt"]), base_dir=tmp_path)
    assert adapter.run(SandboxRequest(), {name: b"x"}, DataEgressPolicy()).failure == "INPUT_NAME_INVALID"
    adapter = SimulatedSandboxAdapter(_echo_to([name]), base_dir=tmp_path)
    assert adapter.run(SandboxRequest(), {"in.txt": b"x"}, DataEgressPolicy()).failure == "ARTIFACT_NAME_INVALID"


def test_cr05_case_insensitive_collisions_across_inputs_and_outputs_are_refused(tmp_path: Path) -> None:
    run = SimulatedSandboxAdapter(_echo_to(["out.txt"]), base_dir=tmp_path).run(
        SandboxRequest(), {"Report.txt": b"a", "report.txt": b"b"}, DataEgressPolicy())
    assert run.failure == "INPUT_NAME_COLLISION"
    run = SimulatedSandboxAdapter(_echo_to(["a.txt", "A.TXT"]), base_dir=tmp_path).run(
        SandboxRequest(), {"in.txt": b"x"}, DataEgressPolicy())
    assert run.failure == "ARTIFACT_NAME_COLLISION"
    run = SimulatedSandboxAdapter(_echo_to(["IN.txt"]), base_dir=tmp_path).run(
        SandboxRequest(), {"in.txt": b"x"}, DataEgressPolicy())
    assert run.failure == "ARTIFACT_NAME_COLLISION"
    ok = SimulatedSandboxAdapter(_echo_to(["result.txt"]), base_dir=tmp_path).run(
        SandboxRequest(), {"in.txt": b"x"}, DataEgressPolicy())
    assert ok.failure is None


# -- UPLOAD-BUFFER -------------------------------------------------------------------------------

def test_cr05_single_oversized_chunk_is_refused_before_it_is_copied(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    copied: list[int] = []

    class Tracking(bytearray):
        def extend(self, data: Any) -> None:
            copied.append(len(data))
            super().extend(data)

    class OneChunk:
        def __init__(self, *chunks: bytes) -> None:
            self.chunks = chunks

        async def stream(self) -> AsyncIterator[bytes]:
            for chunk in self.chunks:
                yield chunk

    monkeypatch.setattr(remote_routes, "MAX_UPLOAD_BODY", 1024)
    monkeypatch.setattr(remote_routes, "bytearray", Tracking, raising=False)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(remote_routes.bounded_body(OneChunk(b"A" * 1025)))  # type: ignore[arg-type]
    assert refused.value.status_code == 413 and copied == []
    with pytest.raises(HTTPException):
        asyncio.run(remote_routes.bounded_body(OneChunk(b"A" * 1000, b"B" * 25)))  # type: ignore[arg-type]
    assert copied == [1000]
    assert asyncio.run(remote_routes.bounded_body(OneChunk(b"A" * 1024))) == b"A" * 1024  # type: ignore[arg-type]


# -- NODE-REASON-LEAK / NODE-ERROR-SCHEMA --------------------------------------------------------

@pytest.fixture
def node(tmp_path: Path) -> Iterator[Node]:
    created = make_node(tmp_path)
    yield created
    created.runtime.jobs.shutdown()


def test_cr05_descriptor_reasons_are_mapped_to_local_codes(node: Node, tmp_path: Path) -> None:
    escape = "\x1b]52;c;" + CREDENTIAL_VALUE + "\x07"

    def noisy(method: str, path: str, response: httpx.Response) -> httpx.Response:
        def mutate(body: dict[str, Any]) -> None:
            body["descriptor"]["health_reasons"] = [CREDENTIAL_VALUE + " leaked", escape,
                                                    "MODEL_MISSING:" + CREDENTIAL_VALUE,
                                                    "BACKEND_UNAVAILABLE:deterministic"]
            body["descriptor"]["health_state"] = "DEGRADED"

        return json_response(response, mutate) if path == "/capabilities" else response

    events: list[dict[str, Any]] = []
    remove = observability.add_sink(events.append)
    try:
        node.rewrite = noisy
        registry = node.registry(tmp_path)
        evidence = NodeProber(registry, resolver=resolver(), http=node.http).probe(registry.nodes[0])
    finally:
        remove()
    assert evidence["reasons"] == ["NODE_REASON_UNRECOGNIZED", "MODEL_MISSING", "BACKEND_UNAVAILABLE:deterministic"]
    persisted = (registry.state_dir / "probes" / f"{node.node_id}.json").read_text(encoding="utf-8")
    assert CREDENTIAL_VALUE not in persisted and "\\u001b" not in persisted and "\x1b" not in persisted
    assert all(CREDENTIAL_VALUE not in json.dumps(event) for event in events)
    assert events[-1]["pados.reason"] == "NODE_REASON_UNRECOGNIZED"


class _Responder:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self.response

    def close(self) -> None:
        pass


@pytest.mark.parametrize(("response", "reason"), [
    (httpx.Response(403, json=[]), "NODE_ERROR"),
    (httpx.Response(403, json="DENIED"), "NODE_ERROR"),
    (httpx.Response(403, json=None), "NODE_ERROR"),
    (httpx.Response(403, json={"detail": ["NESTED"]}), "NODE_ERROR"),
    (httpx.Response(403, json={"detail": "D" * 1000}), "NODE_ERROR"),
    (httpx.Response(403, content=b"<html>"), "NODE_ERROR"),
    (httpx.Response(200, json=["not", "an", "object"]), "NODE_SCHEMA_INVALID"),
    (httpx.Response(200, json="string"), "NODE_SCHEMA_INVALID"),
    (httpx.Response(200, json={"descriptor": ["x"], "identity_proof": "p"}), "NODE_IDENTITY_MISMATCH"),
])
def test_cr05_malformed_node_responses_become_local_error_codes(
    tmp_path: Path, response: httpx.Response, reason: str
) -> None:
    registration = NodeRegistration(node_id="lcn-" + "a" * 20, endpoint=ENDPOINT,
                                    auth_ref="secretref://env/PADOS_LCN_TEST_CREDENTIAL", authorized=True)
    prober = NodeProber(NodeRegistry(state_dir=tmp_path, nodes=(registration,)),
                        resolver=EnvironmentSecretResolver({"PADOS_LCN_TEST_CREDENTIAL": "c" * 24}),
                        http=lambda _endpoint: _Responder(response))
    evidence = prober.probe(registration)
    assert evidence["state"] == "UNAUTHORIZED" and evidence["reasons"] == [reason]


# -- BACKEND-URL ---------------------------------------------------------------------------------

@pytest.mark.parametrize(("value", "expected"), [
    ("http://127.0.0.1", "http://127.0.0.1"),
    ("http://127.0.0.1:11434", "http://127.0.0.1:11434"),
    ("http://[fd00::1]", "http://[fd00::1]"),
    ("HTTP://LOCALHOST:11434", "http://localhost:11434"),
    ("https://10.0.0.5:8443/v1", "https://10.0.0.5:8443/v1"),
])
def test_cr05_backend_endpoints_are_parsed_like_the_http_client(value: str, expected: str) -> None:
    assert private_http_url(value) == expected
    assert BackendConfig(backend_id="b", kind="ollama", endpoint=value).endpoint == expected
    assert NodeRegistration(node_id="lcn-" + "a" * 20, endpoint=value,
                            auth_ref="secretref://env/X").endpoint == expected.rstrip("/")


@pytest.mark.parametrize("value", [
    "http://10.0.0.5:99999", "http:10.0.0.5", "http:///10.0.0.5", "http://10.0.0.5\\@8.8.8.8",
    "http://10.0.0.5#@8.8.8.8", "http://10.0.0.5?x=1", "http://[::ffff:8.8.8.8]", "ftp://10.0.0.5",
    "http://0x7f.1", "http://u:p@127.0.0.1",
])
def test_cr05_backend_endpoints_refuse_what_the_client_would_send_elsewhere(value: str) -> None:
    assert private_http_url(value) is None
    with pytest.raises(ValidationError):
        BackendConfig(backend_id="b", kind="ollama", endpoint=value)


# -- LEDGER-LOCK-PATH ----------------------------------------------------------------------------

@POSIX_ONLY
def test_cr05_ledger_and_lock_links_are_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text("", encoding="utf-8")
    directory = tmp_path / "ledger"
    directory.mkdir(mode=0o700)
    (directory / "intents.jsonl").symlink_to(outside)
    ledger = IntentLedger(directory / "intents.jsonl")
    with pytest.raises(LedgerPathError, match="LEDGER_PATH_IS_LINK"):
        ledger.append({"intent_id": "a"})
    with pytest.raises(LedgerPathError, match="LEDGER_PATH_IS_LINK"):
        ledger.find("a")
    assert outside.read_text(encoding="utf-8") == ""
    (directory / "intents.jsonl").unlink()
    ledger.lock_path.unlink()
    ledger.lock_path.symlink_to(outside)
    with pytest.raises(LedgerPathError, match="LEDGER_PATH_IS_LINK"):
        ledger.append({"intent_id": "a"})


@POSIX_ONLY
def test_cr05_lock_is_private_and_unreplaceable(tmp_path: Path) -> None:
    ledger = IntentLedger(tmp_path / "ledger" / "intents.jsonl")
    ledger.append({"intent_id": "a"})
    assert (ledger.lock_path.stat().st_mode & 0o777) == 0o600
    os.link(ledger.lock_path, tmp_path / "second-name")
    with pytest.raises(LedgerPathError, match="LEDGER_LOCK_NOT_OWNED"):
        ledger.append({"intent_id": "b"})
    (tmp_path / "second-name").unlink()
    os.chmod(ledger.path.parent, 0o777)
    try:
        with pytest.raises(LedgerPathError, match="LEDGER_DIRECTORY_NOT_PROTECTED"):
            ledger.append({"intent_id": "c"})
        os.chmod(ledger.path.parent, 0o1777)  # sticky: other users cannot replace our lock
        ledger.append({"intent_id": "c"})
    finally:
        os.chmod(ledger.path.parent, 0o700)
    assert ledger.verify()


# -- ACTOR-PLAINTEXT -----------------------------------------------------------------------------

PW = "pass" + "word"


def _actors(**row: Any) -> dict[str, Any]:
    return {"actors": [{"actor_id": "owner", "credential_hash": hashlib.sha256(b"x").hexdigest(), **row}]}


@pytest.mark.parametrize("config", [
    {**_actors(), "metadata": {PW: "plain"}},
    {**_actors(), "push": [{"api_key": "plain"}]},
    {**_actors(), "notification": {"provider": {"auth": {"bearer_token": "plain"}}}},
    _actors(metadata={PW: "plain"}),
])
def test_cr05_nested_plaintext_credentials_are_refused(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="REMOTE_PLAINTEXT_CREDENTIAL_FORBIDDEN"):
        RemoteActorRegistry.from_mapping(config)


@pytest.mark.parametrize("row", [{"display_name": "Owner"}, {"projects": "proj"}, {"may_approve": "yes"}])
def test_cr05_actor_rows_have_a_closed_schema(row: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="REMOTE_ACTOR_FIELD_UNKNOWN|REMOTE_ACTOR_INVALID"):
        RemoteActorRegistry.from_mapping(_actors(**row))


def test_cr05_project_ids_used_as_keys_are_data_not_field_names() -> None:
    config = {**_actors(projects=["secret-token-project"]),
              "project_workspaces": {"secret-token-project": "/srv/pados/w"}}
    assert RemoteActorRegistry.from_mapping(config).get("owner") is not None
