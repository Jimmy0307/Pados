# SPDX-License-Identifier: AGPL-3.0-only
"""Local compute node service (``pados node serve``).

A small FastAPI app following the PADOS API patterns. It runs on the node
host (which may be a different machine from the PADOS control plane), binds
only to loopback or an explicitly allowed private address, and authenticates
every request with the SecretRef-backed node credential.
"""
from __future__ import annotations

import hmac
import re
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response

from pados.compute_node.artifacts import ArtifactIntegrityError, ArtifactStore
from pados.compute_node.backends import (
    BACKEND_KINDS,
    BackendError,
    DiscoveredModel,
    InferenceBackend,
    http_transport,
)
from pados.compute_node.config import NodeConfig
from pados.compute_node.identity import NodeIdentity, identity_proof, load_or_create_identity
from pados.compute_node.jobs import ExecuteRequest, JobManager, NodeRejection
from pados.compute_node.manifest import (
    IMPLEMENTED_CAPABILITIES,
    ModelAvailability,
    ModelManifest,
    NodeDescriptor,
    NodeState,
)
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretResolver

DISCOVERY_TTL_SECONDS = 10.0


class NodeRuntime:
    """Everything the node service needs, built once from configuration."""

    def __init__(self, config: NodeConfig, *, resolver: SecretResolver | None = None,
                 backends: Mapping[str, InferenceBackend] | None = None) -> None:
        from pados.compute_node.config import detect_platform

        resolver = resolver or EnvironmentSecretResolver()
        credential = resolver.resolve(config.credential_ref)
        if credential is None:
            raise PermissionError("NODE_CREDENTIAL_UNAVAILABLE")
        self._credential = credential.reveal()
        unsupported = set(config.capabilities) - IMPLEMENTED_CAPABILITIES
        if unsupported:
            raise ValueError("NODE_CAPABILITY_NOT_IMPLEMENTED:" + ",".join(sorted(unsupported)))
        self.config = config
        self.identity: NodeIdentity = load_or_create_identity(config.state_dir, now=_now())
        self.platform, self.architecture = detect_platform()
        self.backends = dict(backends) if backends is not None else {
            b.backend_id: self._backend(b, resolver, config) for b in config.backends}
        missing = {m.backend_id for m in config.models} - set(self.backends)
        if missing:
            raise ValueError("MODEL_BACKEND_UNKNOWN")
        self._declared = [ModelManifest(
            model_id=ModelManifest.canonical(m.backend_id, m.backend_model, m.digest or m.version),
            backend=m.backend_id, backend_model=m.backend_model, family=m.family, version=m.version,
            quantization=m.quantization, context_limit_tokens=m.context_limit_tokens, tasks=m.tasks,
            resource_class=m.resource_class, privacy_class=m.privacy_class, digest=m.digest, aliases=m.aliases,
        ) for m in config.models]
        self._discovery: tuple[float, list[ModelManifest], list[str]] | None = None
        self._discovery_lock = threading.Lock()
        self.store = ArtifactStore(config.state_dir / "artifacts")
        self.jobs = JobManager(
            node_id=self.identity.node_id, limits=config.limits, store=self.store, backends=self.backends,
            credential=self._credential, models=lambda: self.models()[0],
            manifest_hash=lambda: self.descriptor().manifest_hash,
            privacy_class=config.privacy_class, capabilities=frozenset(config.capabilities),
        )

    @staticmethod
    def _backend(config: Any, resolver: SecretResolver, node: NodeConfig) -> InferenceBackend:
        kwargs: dict[str, Any] = {}
        if config.endpoint:
            kwargs["endpoint"] = config.endpoint
        if config.artifact_root:
            # only the native kind accepts a root; it re-verifies the served file against these pins per bound call
            kwargs["artifact_root"] = config.artifact_root
            kwargs["pinned_digests"] = pinned_digests(node, config.backend_id)
        if config.credential_ref:
            backend_credential = resolver.resolve(config.credential_ref)
            if backend_credential is None:
                raise PermissionError("BACKEND_CREDENTIAL_UNAVAILABLE")
            kwargs["transport"] = http_transport(backend_credential)
        return BACKEND_KINDS[config.kind](config.backend_id, **kwargs)

    def authenticate(self, authorization: str | None) -> None:
        expected = f"Bearer {self._credential}"
        if authorization is None or not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="NODE_UNAUTHENTICATED")

    def models(self) -> tuple[list[ModelManifest], list[str]]:
        """Declared models with availability derived from live backend discovery."""
        with self._discovery_lock:
            cached = self._discovery
            if cached is not None and time.monotonic() - cached[0] < DISCOVERY_TTL_SECONDS:
                return cached[1], cached[2]
            reasons: list[str] = []
            found: dict[str, dict[str, DiscoveredModel] | None] = {}
            for backend_id, backend in self.backends.items():
                try:
                    found[backend_id] = {d.backend_model: d for d in backend.discover()} if backend.health() else None
                except BackendError:
                    found[backend_id] = None
                if found[backend_id] is None:
                    reasons.append(f"BACKEND_UNAVAILABLE:{backend_id}")
            models = []
            for model in self._declared:
                listing = found.get(model.backend)
                if listing is None:
                    state = ModelAvailability.BACKEND_UNAVAILABLE
                elif model.backend_model not in listing:
                    state = ModelAvailability.MISSING
                elif model.digest and not _digest_matches(model.digest, listing[model.backend_model].digest):
                    state = ModelAvailability.DIGEST_MISMATCH
                else:
                    state = ModelAvailability.AVAILABLE
                if state is not ModelAvailability.AVAILABLE:
                    reasons.append(f"MODEL_{state.value}:{model.model_id}")
                models.append(model.model_copy(update={"availability": state}))
            self._discovery = (time.monotonic(), models, reasons)
            return models, reasons

    def descriptor(self) -> NodeDescriptor:
        models, reasons = self.models()
        load = self.jobs.load() if hasattr(self, "jobs") else None
        available = {task for m in models if m.availability is ModelAvailability.AVAILABLE for task in m.tasks}
        unserved = [c.value for c in self.config.capabilities if c not in available]
        reasons = [*reasons, *(f"CAPABILITY_WITHOUT_AVAILABLE_MODEL:{c}" for c in unserved)]
        state = NodeState.DEGRADED if reasons else NodeState.ONLINE
        if load is not None and load.saturated:
            state = NodeState.BUSY
        from pados.compute_node.manifest import NodeLoad

        return NodeDescriptor(
            node_id=self.identity.node_id, node_name=self.config.node_name, platform=self.platform,
            architecture=self.architecture, endpoint=self.config.endpoint, health_state=state,
            health_reasons=tuple(sorted(set(reasons))), capabilities=self.config.capabilities, models=tuple(models),
            privacy_class=self.config.privacy_class, resource_class=self.config.resource_class,
            accelerators=self.config.accelerators, last_seen=_now(),
            current_load=load or NodeLoad(running=0, queued=0, max_concurrent_jobs=self.config.limits.max_concurrent_jobs,
                                          max_queue_depth=self.config.limits.max_queue_depth),
            limits=self.config.limits,
        )

    def proof(self, nonce: str | None) -> str | None:
        return identity_proof(self._credential, self.identity.node_id, nonce) if nonce else None


def pinned_digests(config: NodeConfig, backend_id: str) -> dict[str, str | None]:
    """backend model -> its pinned digest; a model declared twice with different (or missing) pins has none."""
    pins: dict[str, str | None] = {}
    for model in config.models:
        if model.backend_id != backend_id:
            continue
        digest = model.digest
        if model.backend_model in pins and pins[model.backend_model] != digest:
            digest = None
        pins[model.backend_model] = digest
    return pins


_FULL_DIGEST = re.compile(r"^(?:sha256:)?[0-9a-f]{64}$")


def _digest_matches(declared: str, observed: str | None) -> bool:
    """Full-length equality of two sha256 digests (red-team F-9: a short or empty prefix never matches)."""
    if not observed or not _FULL_DIGEST.fullmatch(declared) or not _FULL_DIGEST.fullmatch(observed):
        return False
    return declared.removeprefix("sha256:") == observed.removeprefix("sha256:")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create_node_app(runtime: NodeRuntime) -> FastAPI:
    app = FastAPI(title="PADOS Local Compute Node", version=runtime.descriptor().runtime_version)

    def auth(authorization: Annotated[str | None, Header()] = None) -> None:
        runtime.authenticate(authorization)

    guarded = [Depends(auth)]

    def rejection(exc: NodeRejection) -> HTTPException:
        return HTTPException(status_code=exc.status, detail=exc.code)

    @app.get("/health", dependencies=guarded)
    def health(nonce: Annotated[str | None, Query(max_length=128)] = None) -> dict[str, Any]:
        descriptor = runtime.descriptor()
        return {"node_id": descriptor.node_id, "protocol": descriptor.protocol,
                "runtime_version": descriptor.runtime_version, "state": descriptor.health_state.value,
                "reasons": list(descriptor.health_reasons), "load": descriptor.current_load.model_dump(),
                "last_seen": descriptor.last_seen, "manifest_hash": descriptor.manifest_hash,
                "identity_proof": runtime.proof(nonce)}

    @app.get("/capabilities", dependencies=guarded)
    def capabilities(nonce: Annotated[str | None, Query(max_length=128)] = None) -> dict[str, Any]:
        descriptor = runtime.descriptor()
        return {"descriptor": descriptor.model_dump(mode="json"), "manifest_hash": descriptor.manifest_hash,
                "identity_proof": runtime.proof(nonce)}

    @app.get("/models", dependencies=guarded)
    def models() -> dict[str, Any]:
        listed, reasons = runtime.models()
        return {"node_id": runtime.identity.node_id, "models": [m.model_dump(mode="json") for m in listed],
                "reasons": reasons}

    @app.post("/execute", status_code=202, dependencies=guarded)
    def execute(request: ExecuteRequest) -> dict[str, Any]:
        try:
            return runtime.jobs.submit(request)
        except NodeRejection as exc:
            raise rejection(exc) from None

    @app.get("/jobs/{job_id}", dependencies=guarded)
    def job(job_id: str) -> dict[str, Any]:
        try:
            return runtime.jobs.get(job_id)
        except NodeRejection as exc:
            raise rejection(exc) from None

    @app.post("/jobs/{job_id}/cancel", dependencies=guarded)
    def cancel(job_id: str) -> dict[str, Any]:
        try:
            return runtime.jobs.cancel(job_id)
        except NodeRejection as exc:
            raise rejection(exc) from None

    @app.get("/artifacts/{artifact_id}", dependencies=guarded)
    def artifact(artifact_id: str) -> Response:
        try:
            payload = runtime.store.get(artifact_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="ARTIFACT_NOT_FOUND") from None
        except ArtifactIntegrityError:
            raise HTTPException(status_code=409, detail="ARTIFACT_INTEGRITY_FAILED") from None
        return Response(content=payload, media_type="application/octet-stream",
                        headers={"X-PADOS-Artifact-SHA256": artifact_id.removeprefix("sha256:")})

    return app
