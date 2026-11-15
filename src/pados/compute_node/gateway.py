# SPDX-License-Identifier: AGPL-3.0-only
"""Generic enterprise HTTP AI gateway backend (``enterprise_gateway``), driven by a declarative contract.

Implements the compute-node ``InferenceBackend`` protocol for a gateway described by a private
``GatewayContract`` (``pados.compute_node.gateway_contract``); it assumes no OpenAI compatibility.

Failure taxonomy (closed codes, ``GatewayFailure``): network unreachable (VPN or private network down), DNS, TLS
(including certificate errors), auth, model not loaded / not found / identity mismatch, rate limited, timeout,
connection lost, not authorized for execution, plus circuit open, unavailable (5xx), invalid response, auth not
configured and write outcome unknown. Provider text never leaves this module; only codes, counters and hashes do.

Retry policy: reads (health, discovery) are retried with exponential backoff and jitter through the existing
``pados.operability.resilience.retry_call``. A chat call is retried ONLY when the contract declares that the gateway
honours an idempotency key; the same key is sent on every attempt. Otherwise a chat call that may have reached the
gateway (read timeout, dropped connection, 500/502/504) is never re-sent: it fails with
``GATEWAY_WRITE_OUTCOME_UNKNOWN`` (cause in the detail) so the caller reconciles instead of duplicating side effects.

Circuit: one in-memory breaker per provider (``pados.compute_node.circuit``). Every attempt, including refusals
before the network, is written as a content-free attempt receipt (``pados.compute-node.gateway-attempt.v1``).

Authority: gateway output is a NON-AUTHORITATIVE observation of an unmeasured model. The policy only narrows what may
be sent: execution is off unless the contract enables it, the data classification must be given and stay within the
contract's ceiling, and RESTRICTED data is never sent. Authorization stays with the compute-node registration.
"""
from __future__ import annotations

import hashlib
import json
import random
import socket
import ssl
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from pados.compute_node.backends import BackendError, BackendOutput, DiscoveredModel, _messages
from pados.compute_node.circuit import CircuitBreaker, circuit_for
from pados.compute_node.gateway_contract import (
    AuthScheme,
    GatewayContract,
    RouteSpec,
    fill_template,
    json_path_one,
    json_path_values,
)
from pados.compute_node.manifest import NodeCapability
from pados.compute_node.receipts import canonical_hash
from pados.enterprise.classification import DataClassification
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretResolver
from pados.operability.errors import WriteOutcome
from pados.operability.resilience import RetryPolicy, capped_timeout, retry_call
from pados.resources.capability_profile import ExecutionConfig

GATEWAY_KIND = "enterprise_gateway"
ATTEMPT_SCHEMA = "pados.compute-node.gateway-attempt.v1"
AUTHORITY = "NON_AUTHORITATIVE_OBSERVATION"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ATTEMPTS_KEPT = 200


class GatewayFailure(StrEnum):
    NETWORK_UNREACHABLE = "GATEWAY_NETWORK_UNREACHABLE"
    DNS_FAILED = "GATEWAY_DNS_FAILED"
    TLS_FAILED = "GATEWAY_TLS_FAILED"
    AUTH_FAILED = "GATEWAY_AUTH_FAILED"
    AUTH_NOT_CONFIGURED = "GATEWAY_AUTH_NOT_CONFIGURED"
    MODEL_NOT_LOADED = "GATEWAY_MODEL_NOT_LOADED"
    MODEL_NOT_FOUND = "GATEWAY_MODEL_NOT_FOUND"
    MODEL_IDENTITY_MISMATCH = "GATEWAY_MODEL_IDENTITY_MISMATCH"
    RATE_LIMITED = "GATEWAY_RATE_LIMITED"
    TIMEOUT = "GATEWAY_TIMEOUT"
    CONNECTION_LOST = "GATEWAY_CONNECTION_LOST"
    UNAVAILABLE = "GATEWAY_UNAVAILABLE"
    RESPONSE_INVALID = "GATEWAY_RESPONSE_INVALID"
    CIRCUIT_OPEN = "GATEWAY_CIRCUIT_OPEN"
    NOT_AUTHORIZED_FOR_EXECUTION = "GATEWAY_NOT_AUTHORIZED_FOR_EXECUTION"
    WRITE_OUTCOME_UNKNOWN = "GATEWAY_WRITE_OUTCOME_UNKNOWN"


# Plain-language explanations (English; the web UI carries the zh-TW text for the same codes).
EXPLANATIONS: dict[str, str] = {
    GatewayFailure.NETWORK_UNREACHABLE: "The gateway's network cannot be reached. Is the VPN or private network up?",
    GatewayFailure.DNS_FAILED: "The gateway's host name could not be resolved. The VPN DNS may be unavailable.",
    GatewayFailure.TLS_FAILED: "The secure connection failed (certificate not trusted, expired or wrong host).",
    GatewayFailure.AUTH_FAILED: "The gateway refused the credential. Check the referenced key and its permissions.",
    GatewayFailure.AUTH_NOT_CONFIGURED: "The environment variable named by the auth reference is not set here.",
    GatewayFailure.MODEL_NOT_LOADED: "The model exists on the gateway but is not loaded right now.",
    GatewayFailure.MODEL_NOT_FOUND: "The gateway does not offer this model.",
    GatewayFailure.MODEL_IDENTITY_MISMATCH: "The gateway answered with a different model than requested; the answer "
                                            "was discarded.",
    GatewayFailure.RATE_LIMITED: "The gateway is rate limiting requests. Wait and try again later.",
    GatewayFailure.TIMEOUT: "The gateway did not answer in time.",
    GatewayFailure.CONNECTION_LOST: "The connection dropped before the answer was complete.",
    GatewayFailure.UNAVAILABLE: "The gateway reported a server-side failure.",
    GatewayFailure.RESPONSE_INVALID: "The gateway's answer did not match the configured contract.",
    GatewayFailure.CIRCUIT_OPEN: "Recent calls failed repeatedly, so PADOS pauses calls for a while (circuit open).",
    GatewayFailure.NOT_AUTHORIZED_FOR_EXECUTION: "This request may not be sent to this gateway (execution disabled, "
                                                 "data classification above its ceiling, or not given).",
    GatewayFailure.WRITE_OUTCOME_UNKNOWN: "The request may have reached the gateway, but no answer came back. It was "
                                          "not re-sent automatically; check before trying again.",
}

# Dependency failures open the circuit; refusals that prove the gateway answered do not.
_CIRCUIT_FAILURES = frozenset({GatewayFailure.NETWORK_UNREACHABLE, GatewayFailure.DNS_FAILED, GatewayFailure.TLS_FAILED,
                               GatewayFailure.TIMEOUT, GatewayFailure.CONNECTION_LOST, GatewayFailure.UNAVAILABLE,
                               GatewayFailure.RESPONSE_INVALID})
_RETRYABLE = frozenset({GatewayFailure.NETWORK_UNREACHABLE, GatewayFailure.DNS_FAILED, GatewayFailure.TIMEOUT,
                        GatewayFailure.CONNECTION_LOST, GatewayFailure.UNAVAILABLE, GatewayFailure.RATE_LIMITED,
                        GatewayFailure.MODEL_NOT_LOADED})


def explain(code: str | None) -> str | None:
    if code is None:
        return None
    return EXPLANATIONS.get(code.split(":", 1)[0])


class GatewayError(BackendError):
    """A gateway failure: closed code, write outcome, retry safety and (bounded) HTTP status and Retry-After."""

    def __init__(self, failure: GatewayFailure, *, write_outcome: WriteOutcome = WriteOutcome.NO,
                 http_status: int | None = None, retry_after: float | None = None,
                 cause: GatewayFailure | None = None) -> None:
        detail: dict[str, Any] = {"write_outcome": write_outcome.value}
        if http_status is not None:
            detail["http_status"] = http_status
        if cause is not None:
            detail["cause"] = cause.value
        super().__init__(failure.value, detail)
        self.failure = failure
        self.write_outcome = write_outcome
        self.http_status = http_status
        self.retry_after = retry_after
        self.cause = cause
        self.retryable = False  # set per operation by the caller's retry predicate


def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen and len(seen) < 10:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


def classify_transport_error(exc: httpx.HTTPError, *, sent: bool) -> GatewayError:
    """httpx failure -> closed code. ``sent``: the request may have reached the gateway (POST body written)."""
    unknown = WriteOutcome.UNKNOWN if sent else WriteOutcome.NO
    chain = _chain(exc)
    text = " ".join(str(item) for item in chain).casefold()
    if isinstance(exc, httpx.ConnectTimeout):
        return GatewayError(GatewayFailure.NETWORK_UNREACHABLE)
    if isinstance(exc, httpx.TimeoutException):
        return GatewayError(GatewayFailure.TIMEOUT, write_outcome=unknown)
    if isinstance(exc, httpx.ConnectError):
        if any(isinstance(item, ssl.SSLError | ssl.CertificateError) for item in chain) \
                or "certificate" in text or "ssl" in text or "tls" in text:
            return GatewayError(GatewayFailure.TLS_FAILED)
        if any(isinstance(item, socket.gaierror) for item in chain) or "getaddrinfo" in text \
                or "name or service not known" in text or "nodename nor servname" in text \
                or "name resolution" in text:
            return GatewayError(GatewayFailure.DNS_FAILED)
        return GatewayError(GatewayFailure.NETWORK_UNREACHABLE)
    return GatewayError(GatewayFailure.CONNECTION_LOST, write_outcome=unknown)


def _retry_after(headers: httpx.Headers, name: str) -> float | None:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None  # an HTTP date is not followed: PADOS's own backoff applies
    return value if 0 <= value <= 86_400 else None


class AttemptRecorder:
    """Content-free attempt receipts: kept in memory (bounded) and, with ``directory``, appended as JSON lines."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory
        self._records: deque[dict[str, Any]] = deque(maxlen=MAX_ATTEMPTS_KEPT)
        self._lock = threading.Lock()

    def record(self, body: dict[str, Any]) -> dict[str, Any]:
        receipt = {"schema": ATTEMPT_SCHEMA, **body, "semantic_authority": False}
        receipt = {**receipt, "receipt_hash": canonical_hash(receipt)}
        with self._lock:
            self._records.append(receipt)
            if self.directory is not None:
                self.directory.mkdir(parents=True, exist_ok=True)
                with (self.directory / f"{body['provider_id']}.jsonl").open("a", encoding="utf-8") as sink:
                    sink.write(json.dumps(receipt, sort_keys=True) + "\n")
        return receipt

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._records)[-limit:]


ClientFactory = Callable[[httpx.Timeout], httpx.Client]


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class GatewayBackend:
    """``InferenceBackend`` for one contract. ``client_factory``, ``sleep`` and ``rand`` exist for tests."""

    kind = GATEWAY_KIND
    enforces_classification = True

    def __init__(self, backend_id: str, *, contract: GatewayContract, resolver: SecretResolver | None = None,
                 client_factory: ClientFactory | None = None, recorder: AttemptRecorder | None = None,
                 sleep: Callable[[float], None] = time.sleep, rand: Callable[[], float] = random.random) -> None:
        self.backend_id = backend_id
        self.contract = contract
        self.provider_id = contract.provider_id
        self.recorder = recorder or AttemptRecorder()
        self._resolver = resolver or EnvironmentSecretResolver()
        self._client_factory = client_factory or self._default_client
        self._sleep, self._rand = sleep, rand
        self.circuit: CircuitBreaker = circuit_for(contract.provider_id,
                                                   failure_threshold=contract.circuit.failure_threshold,
                                                   open_seconds=contract.circuit.open_seconds)
        self.last_rate_limit: dict[str, str] = {}

    # -- transport -------------------------------------------------------------------------------------------------
    def _default_client(self, timeout: httpx.Timeout) -> httpx.Client:
        verify: ssl.SSLContext | bool = True
        if self.contract.tls_ca_file:
            verify = ssl.create_default_context(cafile=self.contract.tls_ca_file)
        return httpx.Client(verify=verify, timeout=timeout, follow_redirects=False, trust_env=False)

    def _auth_headers(self) -> dict[str, str]:
        auth = self.contract.auth
        if auth.scheme is AuthScheme.NONE or auth.auth_ref is None:
            return {}
        value = self._resolver.resolve(auth.auth_ref)
        if value is None:
            raise GatewayError(GatewayFailure.AUTH_NOT_CONFIGURED)
        if auth.scheme is AuthScheme.BEARER:
            return {"authorization": f"Bearer {value.reveal()}"}
        assert auth.header_name is not None  # validated by the contract
        return {auth.header_name: value.reveal()}

    def _exchange(self, route: RouteSpec, body: dict[str, Any] | None, read_seconds: float,
                  extra_headers: dict[str, str]) -> tuple[int, Any, httpx.Headers]:
        headers = {"accept": "application/json", **self._auth_headers(), **extra_headers}
        timeout = httpx.Timeout(capped_timeout(read_seconds), connect=capped_timeout(self.contract.timeouts.connect_seconds))
        url = self.contract.base_url + route.path
        try:
            with self._client_factory(timeout) as client:
                response = client.request(route.method, url, json=body, headers=headers)
                content = response.content
        except httpx.HTTPError as exc:
            raise classify_transport_error(exc, sent=route.method == "POST") from None
        if len(content) > MAX_RESPONSE_BYTES:
            raise GatewayError(GatewayFailure.RESPONSE_INVALID, http_status=response.status_code,
                               write_outcome=WriteOutcome.UNKNOWN if route.method == "POST" else WriteOutcome.NO)
        try:
            document = json.loads(content) if content else None
        except (ValueError, UnicodeDecodeError):
            document = None
        return response.status_code, document, response.headers

    def _status_failure(self, status: int, document: Any, headers: httpx.Headers, *, chat: bool) -> GatewayError:
        mapped = None
        if self.contract.chat.error_code_path is not None:
            raw = json_path_one(document, self.contract.chat.error_code_path)
            if isinstance(raw, str | int):
                mapped = self.contract.chat.error_codes.get(str(raw))
        retry_after = _retry_after(headers, self.contract.rate_limit_headers.retry_after)
        if mapped is not None:
            return GatewayError(GatewayFailure(mapped), http_status=status, retry_after=retry_after)
        if status in (401, 403):
            return GatewayError(GatewayFailure.AUTH_FAILED, http_status=status)
        if status == 429:
            return GatewayError(GatewayFailure.RATE_LIMITED, http_status=status, retry_after=retry_after)
        if status == 404:
            return GatewayError(GatewayFailure.MODEL_NOT_FOUND if chat else GatewayFailure.RESPONSE_INVALID,
                                http_status=status)
        if status == 408:
            return GatewayError(GatewayFailure.TIMEOUT, http_status=status)
        if status == 503:  # an explicit "not now": the request was refused, not processed
            return GatewayError(GatewayFailure.UNAVAILABLE, http_status=status, retry_after=retry_after)
        if status == 504:
            return GatewayError(GatewayFailure.TIMEOUT, http_status=status,
                                write_outcome=WriteOutcome.UNKNOWN if chat else WriteOutcome.NO)
        if status >= 500:
            return GatewayError(GatewayFailure.UNAVAILABLE, http_status=status,
                                write_outcome=WriteOutcome.UNKNOWN if chat else WriteOutcome.NO)
        return GatewayError(GatewayFailure.RESPONSE_INVALID, http_status=status)

    def _remember_rate_limit(self, headers: httpx.Headers) -> None:
        names = self.contract.rate_limit_headers
        seen = {key: headers.get(name) for key, name in (("remaining", names.remaining), ("reset", names.reset))
                if name is not None and headers.get(name) is not None}
        self.last_rate_limit = {key: str(value)[:32] for key, value in seen.items()}

    # -- one attempt with circuit and receipt ---------------------------------------------------------------------
    def _attempt(self, operation: str, route: RouteSpec, body: dict[str, Any] | None, read_seconds: float, *,
                 attempt: int, probe: bool = False, model: str | None = None,
                 idempotency_key: str | None = None) -> tuple[Any, int]:
        chat = operation == "chat"
        started = time.monotonic()
        before = self.circuit.state.value
        extra = {}
        if idempotency_key is not None and self.contract.idempotency.honoured:
            assert self.contract.idempotency.header_name is not None
            extra[self.contract.idempotency.header_name] = idempotency_key
        status: int | None = None
        try:
            if not self.circuit.acquire(probe=probe):
                raise GatewayError(GatewayFailure.CIRCUIT_OPEN)
            try:
                status, document, headers = self._exchange(route, body, read_seconds, extra)
            except GatewayError as exc:
                self._settle(exc)
                raise
            self._remember_rate_limit(headers)
            expected = status == self.contract.health.expected_status if operation == "health" \
                else 200 <= status <= 299
            if not expected:
                failure = self._status_failure(status, document, headers, chat=chat)
                self._settle(failure)
                raise failure
            self.circuit.record_success()
            self._receipt(operation, attempt, started, before, "OK", status, WriteOutcome.YES if chat
                          else WriteOutcome.NO, model, idempotency_key, body)
            return document, status
        except GatewayError as exc:
            self._receipt(operation, attempt, started, before, exc.code, exc.http_status or status, exc.write_outcome,
                          model, idempotency_key, body, retry_after=exc.retry_after)
            raise

    def _settle(self, exc: GatewayError) -> None:
        if exc.failure in _CIRCUIT_FAILURES:
            self.circuit.record_failure(exc.code)
        else:
            self.circuit.record_neutral(exc.code)

    def _receipt(self, operation: str, attempt: int, started: float, before: str, outcome: str, status: int | None,
                 write: WriteOutcome, model: str | None, key: str | None, body: dict[str, Any] | None, *,
                 retry_after: float | None = None) -> None:
        self.recorder.record({
            "provider_id": self.provider_id, "operation": operation, "attempt": attempt, "at": _now(),
            "duration_ms": round((time.monotonic() - started) * 1000, 3), "outcome": outcome, "http_status": status,
            "write_outcome": write.value, "model": model, "circuit_before": before,
            "circuit_after": self.circuit.state.value,
            "idempotency_key_sha256": _sha256(key) if key else None,
            "request_sha256": canonical_hash(body) if body is not None else None,
            "retry_after_seconds": retry_after, "rate_limit": dict(self.last_rate_limit) or None,
        })

    def _refusal(self, operation: str, failure: GatewayFailure, model: str | None) -> GatewayError:
        """A refusal before any network traffic, still receipted (attempt 0, nothing written)."""
        error = GatewayError(failure)
        self._receipt(operation, 0, time.monotonic(), self.circuit.state.value, error.code, None, WriteOutcome.NO,
                      model, None, None)
        return error

    def _with_retry(self, operation: str, call: Callable[[int], Any], *, retry: bool) -> Any:
        policy = RetryPolicy(max_attempts=self.contract.retry.max_attempts if retry else 1,
                             base_delay_s=self.contract.retry.base_delay_seconds,
                             max_delay_s=self.contract.retry.max_delay_seconds)
        attempts: list[int] = []
        retry_after = [0.0]
        failures: list[GatewayError] = []

        def attempt() -> Any:
            attempts.append(len(attempts) + 1)
            try:
                return call(len(attempts))
            except GatewayError as exc:
                retry_after[0] = exc.retry_after or 0.0
                failures.append(exc)
                raise

        def retryable(exc: BaseException) -> bool:
            if not isinstance(exc, GatewayError) or exc.failure not in _RETRYABLE:
                return False
            return (exc.retry_after or 0.0) <= self.contract.retry.max_retry_after_seconds

        def wait(delay: float) -> None:
            self._sleep(max(delay, retry_after[0]))

        try:
            return retry_call(attempt, operation=f"gateway.{operation}", policy=policy, retryable=retryable,
                              sleep=wait, rand=self._rand)
        except GatewayError as exc:
            # The circuit opened during this call's own retries: report what actually failed, not the pause.
            real = [item for item in failures if item.failure is not GatewayFailure.CIRCUIT_OPEN]
            if exc.failure is GatewayFailure.CIRCUIT_OPEN and real:
                raise real[-1] from None
            raise

    # -- InferenceBackend ------------------------------------------------------------------------------------------
    def check_health(self, *, probe: bool = False) -> int:
        """Raise ``GatewayError`` unless the health route answers as declared; ``probe`` may be a half-open trial."""
        route = self.contract.health.route
        _, status = self._with_retry("health", lambda n: self._attempt(
            "health", route, None, self.contract.timeouts.probe_seconds, attempt=n, probe=probe), retry=not probe)
        return int(status)

    def health(self) -> bool:
        try:
            self.check_health()
        except BackendError:
            return False
        return True

    def discover_detailed(self) -> list[dict[str, Any]]:
        spec = self.contract.discovery
        document, _ = self._with_retry("discover", lambda n: self._attempt(
            "discover", spec.route, None, self.contract.timeouts.probe_seconds, attempt=n), retry=True)
        values = json_path_values(document, spec.models_path)
        if not values:
            raise GatewayError(GatewayFailure.RESPONSE_INVALID)
        items = values[0] if len(values) == 1 and isinstance(values[0], list) else values
        found: list[dict[str, Any]] = []
        for item in items:
            name = json_path_one(item, spec.id_field)
            if not isinstance(name, str) or not name or len(name) > 160:
                continue
            digest = json_path_one(item, spec.digest_field)
            state = json_path_one(item, spec.state_field)
            loaded = None if spec.state_field is None else str(state) in spec.loaded_values
            found.append({"model": name, "digest": digest if isinstance(digest, str) and len(digest) <= 160 else None,
                          "loaded": loaded})
        return found

    def discover(self) -> list[DiscoveredModel]:
        return [DiscoveredModel(item["model"], digest=item["digest"]) for item in self.discover_detailed()
                if item["loaded"] is not False]

    def authorize(self, classification: DataClassification | None) -> GatewayFailure | None:
        """The contract's own data policy (it can only narrow; authorization stays with the registration)."""
        policy = self.contract.policy
        if not policy.execution_enabled or classification is None:
            return GatewayFailure.NOT_AUTHORIZED_FOR_EXECUTION
        if classification is DataClassification.RESTRICTED or not policy.max_classification.permits(classification):
            return GatewayFailure.NOT_AUTHORIZED_FOR_EXECUTION
        return None

    def run(self, capability: NodeCapability, backend_model: str, item: dict[str, Any], *,
            max_output_tokens: int, timeout_seconds: float, execution: ExecutionConfig | None = None,
            classification: DataClassification | None = None, idempotency_key: str | None = None) -> BackendOutput:
        if capability is not NodeCapability.LOCAL_INFERENCE:
            raise BackendError("CAPABILITY_UNSUPPORTED")
        if execution is not None:
            # A gateway's application of a measured configuration cannot be confirmed: refused, never assumed.
            raise BackendError("EXECUTION_CONFIG_UNVERIFIABLE:context_window")
        refused = self.authorize(classification)
        if refused is not None:
            raise self._refusal("chat", refused, backend_model)
        messages = _messages(item)
        prompt = "\n".join(m["content"] for m in messages if m["role"] != "system")
        if (sum(len(m["content"]) for m in messages) // 4) + max_output_tokens > self.contract.max_context_tokens:
            raise BackendError("INPUT_BUDGET_EXCEEDED")
        body = fill_template(self.contract.chat.request_template, {
            "model": backend_model, "messages": messages, "prompt": prompt, "max_output_tokens": max_output_tokens,
            "temperature": 0})
        honoured = self.contract.idempotency.honoured
        key = (idempotency_key or f"pados-{uuid.uuid4().hex}") if honoured else None
        read_seconds = min(self.contract.timeouts.read_seconds, timeout_seconds)
        attempts: list[int] = []

        def call(n: int) -> Any:
            attempts.append(n)
            return self._attempt("chat", self.contract.chat.route, body, read_seconds, attempt=n,
                                 model=backend_model, idempotency_key=key)

        try:
            document, _ = self._with_retry("chat", call, retry=honoured)
        except GatewayError as exc:
            if exc.write_outcome is WriteOutcome.UNKNOWN and not honoured:
                raise GatewayError(GatewayFailure.WRITE_OUTCOME_UNKNOWN, write_outcome=WriteOutcome.UNKNOWN,
                                   http_status=exc.http_status, cause=exc.failure) from None
            raise
        return self._output(document, backend_model, key, len(attempts))

    def _output(self, document: Any, backend_model: str, key: str | None, attempts: int) -> BackendOutput:
        spec = self.contract.chat
        reported = json_path_one(document, spec.model_path)
        if spec.model_path is not None and reported != backend_model:
            raise GatewayError(GatewayFailure.MODEL_IDENTITY_MISMATCH, write_outcome=WriteOutcome.YES)
        text = json_path_one(document, spec.text_path)
        if not isinstance(text, str):
            raise GatewayError(GatewayFailure.RESPONSE_INVALID, write_outcome=WriteOutcome.YES)
        if not text.strip():
            raise BackendError("EMPTY_COMPLETION")
        finish = json_path_one(document, spec.finish_reason_path)
        usage = {name: value for name, path in (("prompt_tokens", spec.usage_prompt_path),
                                                ("completion_tokens", spec.usage_completion_path))
                 if isinstance(value := json_path_one(document, path), int) and not isinstance(value, bool)}
        runtime: dict[str, Any] = {
            "backend": self.kind, "backend_model": backend_model, "provider_id": self.provider_id,
            "authority": AUTHORITY, "measured": False, "attempts": attempts, "usage": usage,
            "truncated": isinstance(finish, str) and finish in spec.truncated_values,
            "idempotency_key_sha256": _sha256(key) if key else None,
        }
        if isinstance(finish, str) and len(finish) <= 32:
            runtime["done_reason"] = finish
        return BackendOutput(text, runtime)

    def status(self) -> dict[str, Any]:
        """Circuit, last rate-limit headers and recent attempts (no content, no credential)."""
        return {"provider_id": self.provider_id, "circuit": self.circuit.snapshot(),
                "rate_limit": dict(self.last_rate_limit), "recent_attempts": self.recorder.recent()}


def gateway_backend(backend_id: str, *, contract_path: str, resolver: SecretResolver | None = None,
                    receipts_dir: Path | None = None, **_: Any) -> GatewayBackend:
    """``BACKEND_KINDS`` factory: the contract is read from the private file named by node configuration."""
    from pados.compute_node.gateway_contract import load_gateway_contract

    return GatewayBackend(backend_id, contract=load_gateway_contract(contract_path), resolver=resolver,
                          recorder=AttemptRecorder(receipts_dir))
