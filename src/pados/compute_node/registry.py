# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS-side node registration, authorization and evidence-derived probes.

reachable != registered != authorized != authorized capability. A node is
usable only when (1) PADOS has an explicit registration, (2) the registration
is authorized, (3) a probe proves the node's identity with the SecretRef-backed
credential, and (4) the requested capability, classification and project are
inside the registration. Health comes only from a successful probe.
"""
from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from pados.compute_node.config import private_http_url
from pados.compute_node.identity import identity_proof, verify
from pados.compute_node.identity_providers import (
    IdentityProfile,
    SecurityProfile,
    assert_profile_allowed,
)
from pados.compute_node.manifest import ModelAvailability, NodeCapability, NodeDescriptor, NodeState
from pados.enterprise import observability
from pados.enterprise.classification import DataClassification
from pados.enterprise.secrets import (
    EnvironmentSecretResolver,
    SecretBoundary,
    SecretResolver,
    is_secret_ref,
)

REGISTRY_SCHEMA = "pados.compute-nodes.registry.v1"
MAX_NODE_ERROR_CODE = 64
HttpFactory = Callable[[str], httpx.Client]


class NodeRegistration(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(pattern=r"^lcn-[a-f0-9]{20}$")
    node_name: str = Field(default="", max_length=80)
    endpoint: str = Field(min_length=1, max_length=200)
    auth_ref: str
    authorized: bool = False
    allowed_capabilities: tuple[NodeCapability, ...] = ()
    max_classification: DataClassification = DataClassification.INTERNAL
    project_ids: tuple[str, ...] = ()
    allowed_models: tuple[str, ...] = ()
    security_profile: SecurityProfile = SecurityProfile.TRUSTED_LAN
    identity_profile: IdentityProfile = IdentityProfile.HMAC_SHARED_SECRET
    # Optional link to the one machine inventory (pados.machines); registrations without it load unchanged.
    machine_id: str | None = Field(default=None, pattern=r"^mach-[a-f0-9]{20}$")

    @field_validator("endpoint")
    @classmethod
    def private_endpoint(cls, value: str) -> str:
        validated = private_http_url(value)
        if validated is None:
            raise ValueError("NODE_ENDPOINT_MUST_BE_PRIVATE_WITHOUT_CREDENTIALS")
        return validated.rstrip("/")

    @field_validator("auth_ref")
    @classmethod
    def secret_ref(cls, value: str) -> str:
        if not is_secret_ref(value):
            raise ValueError("SECRET_REFERENCE_REQUIRED")
        return value

    @model_validator(mode="after")
    def identity_matches_profile(self) -> NodeRegistration:
        try:
            assert_profile_allowed(self.security_profile, self.identity_profile)
        except PermissionError as exc:
            raise ValueError(str(exc)) from None
        if self.security_profile is SecurityProfile.LOCAL_SINGLE_USER and not _is_loopback(self.endpoint):
            raise ValueError("LOCAL_SINGLE_USER_REQUIRES_LOOPBACK_ENDPOINT")
        return self

    def admits_project(self, project_id: str) -> bool:
        return "*" in self.project_ids or project_id in self.project_ids


def _is_loopback(endpoint: str) -> bool:
    host = endpoint.split("//", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0].strip("[]")
    return host in {"127.0.0.1", "localhost", "::1"}


class NodeRegistry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = REGISTRY_SCHEMA
    state_dir: Path
    probe_max_age_seconds: float = Field(default=300, gt=0, le=86_400)
    probe_timeout_seconds: float = Field(default=3.0, gt=0, le=60)
    nodes: tuple[NodeRegistration, ...] = ()

    @field_validator("schema_version")
    @classmethod
    def schema_supported(cls, value: str) -> str:
        if value != REGISTRY_SCHEMA:
            raise ValueError("NODE_REGISTRY_SCHEMA_INVALID")
        return value

    @classmethod
    def load(cls, path: str | Path) -> NodeRegistry:
        registry = cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
        ids = [node.node_id for node in registry.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("NODE_REGISTRATION_DUPLICATE")
        return registry

    def get(self, node_id: str) -> NodeRegistration:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise PermissionError("NODE_NOT_REGISTERED")


class NodeClientError(RuntimeError):
    def __init__(self, code: str, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def default_http(endpoint: str) -> httpx.Client:
    return httpx.Client(base_url=endpoint)


def _error_detail(response: httpx.Response) -> str:
    """The ``detail`` of an error body only when the body is a JSON object with a short string detail."""
    try:
        body = response.json()
    except ValueError:
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) and len(detail) <= MAX_NODE_ERROR_CODE else ""


# Reason codes a node runtime emits (``compute_node/service.py``), as ``CODE`` or ``CODE:<identifier>``.
_NODE_REASON_CODES = frozenset({"BACKEND_UNAVAILABLE", "CAPABILITY_WITHOUT_AVAILABLE_MODEL",
                                *(f"MODEL_{state.value}" for state in ModelAvailability)})


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,199}")


def local_reasons(descriptor: NodeDescriptor) -> list[str]:
    """Map node-reported health reasons onto local codes; no remote text is kept verbatim.

    A suffix survives only if it names something PADOS already validated in this descriptor
    (a model id, a backend id or a capability); anything else becomes the bare code, and an
    unknown code becomes ``NODE_REASON_UNRECOGNIZED``.
    """
    known = {m.model_id for m in descriptor.models} | {m.backend for m in descriptor.models} | {
        c.value for c in descriptor.capabilities}
    mapped: list[str] = []
    for reason in descriptor.health_reasons[:32]:
        code, _, suffix = reason.partition(":")
        if code not in _NODE_REASON_CODES:
            item = "NODE_REASON_UNRECOGNIZED"
        else:
            item = f"{code}:{suffix}" if suffix in known and _IDENTIFIER.fullmatch(suffix) else code
        if item not in mapped:
            mapped.append(item)
    return mapped


class NodeClient:
    """Authenticated client for one registered node; the credential never leaves it."""

    def __init__(self, registration: NodeRegistration, credential: str, *, http: HttpFactory | None = None,
                 timeout: float = 3.0) -> None:
        self.registration = registration
        self._credential = credential
        self._http = (http or default_http)(registration.endpoint)
        self._timeout = timeout

    def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None,
                 params: dict[str, str] | None = None, raw: bool = False, timeout: float | None = None) -> Any:
        try:
            response = self._http.request(method, path, json=json_body, params=params,
                                          headers={"authorization": f"Bearer {self._credential}"},
                                          timeout=timeout or self._timeout)
        except httpx.TimeoutException:
            raise NodeClientError("NODE_TIMEOUT") from None
        except httpx.HTTPError:
            raise NodeClientError("NODE_OFFLINE") from None
        if response.status_code == 401:
            raise NodeClientError("NODE_AUTHENTICATION_FAILED", 401)
        if response.status_code >= 400:
            raise NodeClientError(self._error_code(_error_detail(response)) or f"NODE_HTTP_{response.status_code}",
                                  response.status_code)
        if raw:
            return response
        try:
            body = response.json()
        except ValueError:
            raise NodeClientError("NODE_SCHEMA_INVALID") from None
        if not isinstance(body, dict):  # every node endpoint answers with a JSON object
            raise NodeClientError("NODE_SCHEMA_INVALID")
        return body

    def _error_code(self, detail: str) -> str:
        """A node-supplied error detail is kept only as a short code that cannot carry the credential:
        it is persisted in probe evidence, logged and shown in Mission Control."""
        if not (detail.replace("_", "").replace(":", "").isalnum() and detail.isupper()):
            return "NODE_ERROR"
        if len(detail) > MAX_NODE_ERROR_CODE or (self._credential and self._credential.upper() in detail.upper()):
            return "NODE_ERROR"
        return detail

    def verified(self, path: str) -> dict[str, Any]:
        """GET a descriptor endpoint and verify identity and credential possession."""
        nonce = secrets.token_hex(16)
        body = self._request("GET", path, params={"nonce": nonce})
        descriptor = body.get("descriptor")
        node_id = body.get("node_id") or (descriptor.get("node_id") if isinstance(descriptor, dict) else None)
        if node_id != self.registration.node_id:
            raise NodeClientError("NODE_IDENTITY_MISMATCH")
        if not verify(identity_proof(self._credential, self.registration.node_id, nonce), body.get("identity_proof")):
            raise NodeClientError("NODE_IDENTITY_PROOF_INVALID")
        return dict(body)

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        return dict(self._request("POST", "/execute", json_body=request))

    def job(self, job_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/jobs/{job_id}"))

    def cancel(self, job_id: str) -> dict[str, Any]:
        return dict(self._request("POST", f"/jobs/{job_id}/cancel"))

    def artifact(self, artifact_id: str) -> tuple[bytes, str | None]:
        response = self._request("GET", f"/artifacts/{artifact_id}", raw=True, timeout=30)
        return bytes(response.content), response.headers.get("X-PADOS-Artifact-SHA256")

    def close(self) -> None:
        self._http.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class NodeProber:
    """Derives node state from probe evidence and persists it for doctor/Mission Control."""

    def __init__(self, registry: NodeRegistry, *, resolver: SecretResolver | None = None,
                 http: HttpFactory | None = None) -> None:
        self.registry = registry
        self.boundary = SecretBoundary(resolver or EnvironmentSecretResolver())
        self.http = http

    def client(self, registration: NodeRegistration, credential: str) -> NodeClient:
        return NodeClient(registration, credential, http=self.http, timeout=self.registry.probe_timeout_seconds)

    def probe(self, registration: NodeRegistration) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "node_id": registration.node_id, "node_name": registration.node_name, "endpoint": registration.endpoint,
            "probed_at": _now(), "registered": True, "authorized": registration.authorized,
            "reachable": False, "authenticated": False, "identity_verified": False,
            "manifest_valid": False, "reported_state": None, "reasons": [], "latency_ms": None,
            "manifest_hash": None, "descriptor": None,
        }
        started = time.monotonic()
        if registration.identity_profile is not IdentityProfile.HMAC_SHARED_SECRET:
            # Designed hardened profile; no implementation in Core yet: fail closed.
            evidence.update(state=NodeState.UNAUTHORIZED.value, reasons=["ASYMMETRIC_IDENTITY_NOT_AVAILABLE"])
            return self._store(evidence)
        try:
            with self.boundary.resolved({registration.auth_ref}, authorized_refs={registration.auth_ref}) as values:
                client = self.client(registration, values[registration.auth_ref].reveal())
                try:
                    body = client.verified("/capabilities")
                finally:
                    client.close()
        except PermissionError as exc:
            evidence.update(state=NodeState.UNAUTHORIZED.value, reasons=[str(exc)])
            return self._store(evidence)
        except NodeClientError as exc:
            evidence["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
            if exc.code in {"NODE_OFFLINE", "NODE_TIMEOUT"}:
                evidence.update(state=NodeState.OFFLINE.value, reasons=[exc.code])
            else:
                evidence.update(reachable=True, authenticated=exc.code != "NODE_AUTHENTICATION_FAILED",
                                state=NodeState.UNAUTHORIZED.value, reasons=[exc.code])
            return self._store(evidence)
        evidence.update(reachable=True, authenticated=True, identity_verified=True,
                        latency_ms=round((time.monotonic() - started) * 1000, 3))
        try:
            descriptor = NodeDescriptor.model_validate(body["descriptor"])
            if descriptor.manifest_hash != body.get("manifest_hash"):
                raise ValueError("MANIFEST_HASH_MISMATCH")
        except (KeyError, ValueError, ValidationError):
            evidence.update(state=NodeState.DEGRADED.value, reasons=["NODE_MANIFEST_INVALID"])
            return self._store(evidence)
        reasons = local_reasons(descriptor)
        descriptor = descriptor.model_copy(update={"health_reasons": tuple(reasons)})
        evidence.update(manifest_valid=True, reported_state=descriptor.health_state.value,
                        manifest_hash=descriptor.manifest_hash, descriptor=descriptor.model_dump(mode="json"),
                        reasons=reasons, last_successful_check=evidence["probed_at"])
        state = descriptor.health_state
        if not registration.authorized:
            state = NodeState.UNAUTHORIZED
            evidence["reasons"] = ["NODE_NOT_AUTHORIZED", *evidence["reasons"]]
        evidence["state"] = state.value
        return self._store(evidence)

    def _store(self, evidence: dict[str, Any]) -> dict[str, Any]:
        directory = self.registry.state_dir / "probes"
        directory.mkdir(parents=True, exist_ok=True)
        previous = load_probe(self.registry.state_dir, evidence["node_id"])
        if "last_successful_check" not in evidence:
            evidence["last_successful_check"] = (previous or {}).get("last_successful_check")
        tmp = directory / f".{evidence['node_id']}.json.tmp"
        tmp.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(directory / f"{evidence['node_id']}.json")
        reason = (evidence["reasons"] or [None])[0]
        # Only the local code part is logged, never an identifier suffix.
        observability.emit("node.probe", node_id=evidence["node_id"], node_state=evidence["state"],
                           latency_ms=evidence["latency_ms"], reason=str(reason).split(":", 1)[0] if reason else None)
        return evidence


def load_probe(state_dir: Path, node_id: str) -> dict[str, Any] | None:
    path = state_dir / "probes" / f"{node_id}.json"
    if not path.is_file():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else None
