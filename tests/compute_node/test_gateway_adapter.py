# SPDX-License-Identifier: AGPL-3.0-only
"""Gateway adapter against the simulated gateway over real loopback HTTP(S): discovery, auth, failure taxonomy,
retry rules, circuit transitions, data classification and attempt receipts."""
from __future__ import annotations

import json
import socket
import ssl
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from pados.compute_node.backends import BackendError
from pados.compute_node.circuit import CircuitBreaker, CircuitState, reset_circuits
from pados.compute_node.gateway import AttemptRecorder, GatewayBackend, GatewayError, GatewayFailure
from pados.compute_node.gateway_contract import parse_gateway_contract
from pados.compute_node.jobs import _gateway_call
from pados.compute_node.manifest import NodeCapability
from pados.enterprise.classification import DataClassification
from pados.resources.capability_profile import ExecutionConfig, OutputConstraint, ReasoningMode
from tests.compute_node.gateway_support import (
    KEY_ENV,
    running_sim,
    self_signed_certificate,
    sim_contract,
    sim_control,
    sim_key,
    sim_stats,
)

CHAT = NodeCapability.LOCAL_INFERENCE
PROMPT = {"messages": [{"role": "user", "content": "synthetic probe text 7f3a"}]}


@pytest.fixture(autouse=True)
def _fresh_circuits() -> Iterator[None]:
    reset_circuits()
    yield
    reset_circuits()


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> str:
    value = sim_key()
    monkeypatch.setenv(KEY_ENV, value)
    return value


@pytest.fixture
def sim(key: str) -> Iterator[int]:
    with running_sim(key) as port:
        yield port


def backend(port: int, tmp_path: Path | None = None, **overrides: Any) -> GatewayBackend:
    contract = parse_gateway_contract(sim_contract(f"http://127.0.0.1:{port}", **overrides))
    return GatewayBackend("gw", contract=contract, recorder=AttemptRecorder(tmp_path), sleep=lambda _: None)


def chat(gateway: GatewayBackend, model: str = "sim-small", *,
         classification: DataClassification | None = DataClassification.INTERNAL, **kwargs: Any) -> Any:
    return gateway.run(CHAT, model, PROMPT, max_output_tokens=32, timeout_seconds=10,
                       classification=classification, **kwargs)


def failure(call: Any) -> GatewayError:
    with pytest.raises(GatewayError) as caught:
        call()
    return caught.value


def test_health_discovery_and_model_identity(sim: int) -> None:
    gateway = backend(sim)
    assert gateway.health() is True
    detailed = {item["model"]: item for item in gateway.discover_detailed()}
    assert detailed["sim-small"]["loaded"] is True and detailed["sim-small"]["digest"].startswith("sha256:")
    assert detailed["sim-large"]["loaded"] is False
    assert [m.backend_model for m in gateway.discover()] == ["sim-small"]  # a cold model is not offered


def test_chat_is_a_non_authoritative_observation_with_content_free_receipts(sim: int, tmp_path: Path) -> None:
    gateway = backend(sim, tmp_path)
    output = chat(gateway)
    assert output.content.startswith("SIMULATED: ")
    assert output.runtime["authority"] == "NON_AUTHORITATIVE_OBSERVATION" and output.runtime["measured"] is False
    assert output.runtime["usage"] == {"prompt_tokens": 4, "completion_tokens": 2}
    assert output.runtime["done_reason"] == "end" and output.runtime["truncated"] is False
    receipts = (tmp_path / "sim-gateway.jsonl").read_text(encoding="utf-8")
    assert "synthetic probe text" not in receipts and "SIMULATED" not in receipts
    last = json.loads(receipts.splitlines()[-1])
    assert last["operation"] == "chat" and last["outcome"] == "OK" and last["write_outcome"] == "YES"
    assert last["semantic_authority"] is False and len(last["request_sha256"]) == 64
    assert gateway.last_rate_limit == {"remaining": "99"}


def test_auth_failures(sim: int, monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = backend(sim)
    monkeypatch.setenv(KEY_ENV, "sim-wrong")
    assert failure(lambda: gateway.check_health()).failure is GatewayFailure.AUTH_FAILED
    monkeypatch.delenv(KEY_ENV)
    before = sim_stats(sim)["unauthorized"]
    assert failure(lambda: chat(gateway)).failure is GatewayFailure.AUTH_NOT_CONFIGURED
    assert sim_stats(sim)["unauthorized"] == before  # nothing was sent without a credential


def test_forbidden_is_an_auth_failure_and_does_not_open_the_circuit(sim: int) -> None:
    gateway = backend(sim)
    sim_control(sim, "forbid")
    for _ in range(3):
        error = failure(lambda: chat(gateway))
        assert error.failure is GatewayFailure.AUTH_FAILED and error.http_status == 403
    assert gateway.circuit.state is CircuitState.CLOSED


def test_model_errors(sim: int) -> None:
    gateway = backend(sim)
    assert failure(lambda: chat(gateway, "sim-missing")).failure is GatewayFailure.MODEL_NOT_FOUND
    assert failure(lambda: chat(gateway, "sim-large")).failure is GatewayFailure.MODEL_NOT_LOADED
    sim_control(sim, "wrong_model", times=1)
    error = failure(lambda: chat(gateway))
    assert error.failure is GatewayFailure.MODEL_IDENTITY_MISMATCH


def test_rate_limit_is_reported_and_a_plain_chat_is_not_retried(sim: int, tmp_path: Path) -> None:
    gateway = backend(sim, tmp_path)
    sim_control(sim, "rate_limited")
    before = sim_stats(sim)["generate_requests"]
    error = failure(lambda: chat(gateway))
    assert error.failure is GatewayFailure.RATE_LIMITED and error.retry_after == 1.0
    assert sim_stats(sim)["generate_requests"] == before + 1
    last = gateway.recorder.recent()[-1]
    assert last["retry_after_seconds"] == 1.0 and last["rate_limit"] == {"remaining": "0"}


def test_timeout_on_chat_is_an_unknown_outcome_and_never_resent(sim: int) -> None:
    gateway = backend(sim, timeouts={"connect_seconds": 1, "read_seconds": 0.5, "probe_seconds": 1})
    sim_control(sim, "slow", delay=1.5)
    before = sim_stats(sim)["generate_requests"]
    error = failure(lambda: chat(gateway))
    assert error.failure is GatewayFailure.WRITE_OUTCOME_UNKNOWN and error.cause is GatewayFailure.TIMEOUT
    assert error.write_outcome.value == "UNKNOWN"
    time.sleep(1.2)
    assert sim_stats(sim)["generate_requests"] == before + 1


def test_dropped_connection_is_never_resent_without_an_honoured_key(sim: int) -> None:
    gateway = backend(sim)
    sim_control(sim, "drop", times=1)
    before = sim_stats(sim)
    error = failure(lambda: chat(gateway))
    assert error.failure is GatewayFailure.WRITE_OUTCOME_UNKNOWN and error.cause is GatewayFailure.CONNECTION_LOST
    after = sim_stats(sim)
    assert after["generate_requests"] == before["generate_requests"] + 1
    assert after["side_effects"] == before["side_effects"] + 1


def test_honoured_idempotency_key_retries_without_a_second_side_effect(sim: int) -> None:
    gateway = backend(sim, idempotency={"header_name": "Idempotency-Key", "honoured": True})
    sim_control(sim, "drop", times=1)
    before = sim_stats(sim)
    output = chat(gateway, idempotency_key="job-1:0")
    after = sim_stats(sim)
    assert output.runtime["attempts"] == 2
    assert after["generate_requests"] == before["generate_requests"] + 2
    assert after["side_effects"] == before["side_effects"] + 1 and after["replayed"] == before["replayed"] + 1
    assert after["keys"]["job-1:0"] == 2
    keys = {r["idempotency_key_sha256"] for r in gateway.recorder.recent() if r["operation"] == "chat"}
    assert len(keys) == 1  # the same key on every attempt


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_network_unreachable_reads_are_retried_and_receipted(key: str, tmp_path: Path) -> None:
    gateway = backend(_closed_port(), tmp_path, circuit={"failure_threshold": 10, "open_seconds": 1})
    error = failure(lambda: gateway.check_health())
    assert error.failure is GatewayFailure.NETWORK_UNREACHABLE
    attempts = [r for r in gateway.recorder.recent() if r["operation"] == "health"]
    assert [r["attempt"] for r in attempts] == [1, 2, 3]
    assert all(r["outcome"] == "GATEWAY_NETWORK_UNREACHABLE" and r["write_outcome"] == "NO" for r in attempts)


def _raising(exc: Exception) -> Any:
    def factory(timeout: httpx.Timeout) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            raise exc
        return httpx.Client(transport=httpx.MockTransport(handler), timeout=timeout)
    return factory


def test_dns_failure_is_classified(key: str) -> None:
    cause = httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    cause.__cause__ = socket.gaierror(11001, "getaddrinfo failed")
    contract = parse_gateway_contract(sim_contract("https://gateway.example.invalid"))
    gateway = GatewayBackend("gw", contract=contract, client_factory=_raising(cause), sleep=lambda _: None)
    assert failure(lambda: gateway.check_health()).failure is GatewayFailure.DNS_FAILED


def test_tls_failure_against_a_plain_http_endpoint(sim: int) -> None:
    gateway = backend(sim)
    https = parse_gateway_contract(sim_contract(f"https://127.0.0.1:{sim}"))
    gateway = GatewayBackend("gw", contract=https, sleep=lambda _: None)
    assert failure(lambda: gateway.check_health()).failure is GatewayFailure.TLS_FAILED


def test_tls_untrusted_certificate_then_trusted_private_ca(key: str, tmp_path: Path) -> None:
    pair = self_signed_certificate(tmp_path)
    if pair is None:
        pytest.skip("openssl is not available to make a test-only certificate")
    cert, private = pair
    with running_sim(key, cert=str(cert), key_file=str(private)) as port:
        untrusted = GatewayBackend("gw", contract=parse_gateway_contract(sim_contract(f"https://127.0.0.1:{port}")),
                                   sleep=lambda _: None)
        assert failure(lambda: untrusted.check_health()).failure is GatewayFailure.TLS_FAILED
        reset_circuits()
        trusted = GatewayBackend("gw", contract=parse_gateway_contract(
            sim_contract(f"https://127.0.0.1:{port}", tls_ca_file=str(cert))), sleep=lambda _: None)
        assert trusted.check_health() == 200
        assert chat(trusted).content.startswith("SIMULATED: ")


def test_circuit_opens_refuses_without_traffic_then_half_opens_and_closes(sim: int) -> None:
    gateway = backend(sim)
    sim_control(sim, "unavailable")
    error = failure(lambda: gateway.check_health())  # attempts 1 and 2 fail -> OPEN; attempt 3 refused
    assert error.failure is GatewayFailure.UNAVAILABLE  # the real cause, not the pause
    assert [r["outcome"] for r in gateway.recorder.recent()] == ["GATEWAY_UNAVAILABLE", "GATEWAY_UNAVAILABLE",
                                                                 "GATEWAY_CIRCUIT_OPEN"]
    assert gateway.circuit.state is CircuitState.OPEN
    before = sim_stats(sim)["health"]
    assert failure(lambda: chat(gateway)).failure is GatewayFailure.CIRCUIT_OPEN
    assert sim_stats(sim)["health"] == before
    sim_control(sim, "normal")  # the gateway is back (for example the VPN reconnected)
    time.sleep(0.6)
    assert gateway.circuit.state is CircuitState.HALF_OPEN
    assert gateway.check_health() == 200
    snapshot = gateway.status()["circuit"]
    assert snapshot["state"] == "CLOSED"
    assert [t["to"] for t in snapshot["transitions"]] == ["OPEN", "HALF_OPEN", "CLOSED"]


def test_operator_probe_is_the_half_open_trial_while_open(sim: int) -> None:
    gateway = backend(sim, circuit={"failure_threshold": 1, "open_seconds": 600})
    sim_control(sim, "unavailable", times=1)
    failure(lambda: gateway.check_health())
    assert gateway.circuit.state is CircuitState.OPEN
    assert gateway.check_health(probe=True) == 200
    assert gateway.circuit.state is CircuitState.CLOSED


def test_breaker_unit_transitions() -> None:
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=2, open_seconds=10, clock=lambda: now[0])
    breaker.record_failure("X")
    assert breaker.state is CircuitState.CLOSED
    breaker.record_failure("X")
    assert breaker.state is CircuitState.OPEN and not breaker.acquire()
    now[0] = 10.0
    assert breaker.acquire() and not breaker.acquire()  # exactly one trial
    breaker.record_failure("X")
    assert breaker.state is CircuitState.OPEN
    now[0] = 20.0
    assert breaker.acquire()
    breaker.record_neutral("GATEWAY_AUTH_FAILED")  # the gateway answered: it is up
    assert breaker.state is CircuitState.CLOSED


@pytest.mark.parametrize("classification", [DataClassification.CONFIDENTIAL, DataClassification.RESTRICTED, None])
def test_data_classification_refusal_sends_nothing(sim: int, classification: DataClassification | None) -> None:
    gateway = backend(sim)
    before = sim_stats(sim)["generate_requests"]
    error = failure(lambda: chat(gateway, classification=classification))
    assert error.failure is GatewayFailure.NOT_AUTHORIZED_FOR_EXECUTION
    assert sim_stats(sim)["generate_requests"] == before
    assert gateway.recorder.recent()[-1]["attempt"] == 0  # the refusal is receipted too


def test_execution_disabled_by_default_and_measured_config_refused(sim: int) -> None:
    disabled = backend(sim, policy={"max_classification": "INTERNAL"})
    assert failure(lambda: chat(disabled)).failure is GatewayFailure.NOT_AUTHORIZED_FOR_EXECUTION
    measured = ExecutionConfig(context_window=8192, reasoning_mode=ReasoningMode.DISABLED, temperature=0, seed=42,
                               output_constraint=OutputConstraint.UNCONSTRAINED, max_output_tokens=32)
    with pytest.raises(BackendError, match="EXECUTION_CONFIG_UNVERIFIABLE"):
        chat(backend(sim), execution=measured)


def test_node_jobs_pass_classification_and_a_stable_key_only_to_the_gateway(sim: int) -> None:
    class Request:
        classification = DataClassification.INTERNAL
        request_id = "req-1"

    assert _gateway_call(backend(sim), Request(), 2) == {"classification": DataClassification.INTERNAL,  # type: ignore[arg-type]
                                                        "idempotency_key": "req-1:2"}
    assert _gateway_call(object(), Request(), 0) == {}  # type: ignore[arg-type]


def test_ssl_module_is_the_tls_classifier_source() -> None:
    cause = httpx.ConnectError("handshake failed")
    cause.__cause__ = ssl.SSLCertVerificationError(1, "certificate verify failed")
    from pados.compute_node.gateway import classify_transport_error

    assert classify_transport_error(cause, sent=False).failure is GatewayFailure.TLS_FAILED
    assert classify_transport_error(httpx.ReadTimeout("t"), sent=True).write_outcome.value == "UNKNOWN"
    assert classify_transport_error(httpx.ConnectTimeout("t"), sent=True).failure is GatewayFailure.NETWORK_UNREACHABLE
