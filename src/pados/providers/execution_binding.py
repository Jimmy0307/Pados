# SPDX-License-Identifier: AGPL-3.0-only
"""Measured execution binding for the consumer inference path (graph cognition and other chat callers).

Composes only existing authorities; adds no registry, router or authorization system:

1. the operator ``ModelCatalog`` (``ModelCatalogStore`` at ``PADOS_MODEL_CATALOG``) for the REGISTERED measured
   profile of the configured runtime family and artifact reference (the reference is only a lookup key),
2. ``LiveLocalState`` + ``bind_live`` for the live identity check (manifest digest present, measured runtime
   version, measured device, fresh observations),
3. ``registration_authorizes`` over the compute-node registrations (``PADOS_COMPUTE_NODES_CONFIG``); the
   registration must explicitly name the profile's immutable ``model_ref`` (revoking = removing it),
4. one generic intent -> ``CapabilityRequirement`` table, ``required_execution``, ``assess`` and
   ``check_execution`` for the measured execution configuration.

Every missing or mismatched element is a typed ``ExecutionRefusal``; nothing falls back to runtime defaults.
This module names no model, vendor or runtime parameter: runtime translation stays in the compute-node backend.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from pados.capability.inference import registration_authorizes
from pados.capability.seams import InferenceRequest
from pados.compute_node.registry import NodeRegistration, NodeRegistry
from pados.config import Settings
from pados.enterprise.classification import DataClassification
from pados.local_intelligence.catalog_store import CatalogStoreError, ModelCatalogStore
from pados.local_intelligence.live import LiveLocalState
from pados.local_intelligence.runtimes import default_runtime_endpoints
from pados.resources.capability_profile import (
    BindingPolicy,
    CapabilityFloor,
    CapabilityRequirement,
    ExecutionCompatibility,
    ExecutionConfig,
    LiveRuntimeBinding,
    MeasuredCapability,
    MeasuredExecutionCondition,
    ModelCapabilityProfile,
    assess,
    bind_live,
    check_execution,
    required_execution,
)
from pados.resources.models import ModelCatalog

EVIDENCE_SCHEMA = "pados.providers.measured-execution.v1"
AUTHORIZATION_SOURCE = "COMPUTE_NODE_REGISTRATION"
_STAGE_ID = "consumer-inference"
_MAX_REASONS = 32

MEASURED_PROFILE_STORE_NOT_CONFIGURED = "MEASURED_PROFILE_STORE_NOT_CONFIGURED"
MODEL_CATALOG_STORE_INVALID = "MODEL_CATALOG_STORE_INVALID"
MODEL_PROFILE_NOT_REGISTERED = "MODEL_PROFILE_NOT_REGISTERED"
MODEL_PROFILE_AMBIGUOUS = "MODEL_PROFILE_AMBIGUOUS"
LIVE_BINDING_REFUSED = "LIVE_BINDING_REFUSED"
MODEL_AUTHORIZATION_NOT_CONFIGURED = "MODEL_AUTHORIZATION_NOT_CONFIGURED"
MODEL_AUTHORIZATION_STORE_INVALID = "MODEL_AUTHORIZATION_STORE_INVALID"
MODEL_NOT_AUTHORIZED = "MODEL_NOT_AUTHORIZED"
MEASURED_INELIGIBLE = "MEASURED_INELIGIBLE"
MEASURED_EXECUTION_CONFIG_MISMATCH = "MEASURED_EXECUTION_CONFIG_MISMATCH"
MEASURED_EXECUTION_CONFIG_NOT_APPLIED = "MEASURED_EXECUTION_CONFIG_NOT_APPLIED"
MEASURED_EXECUTION_INTENT_REQUIRED = "MEASURED_EXECUTION_INTENT_REQUIRED"


class ExecutionIntent(StrEnum):
    """What the caller needs from the model, in measured-capability terms (never a model or runtime name)."""

    STRUCTURED_OUTPUT = "STRUCTURED_OUTPUT"
    TEXT_SUMMARY = "TEXT_SUMMARY"


INTENT_REQUIREMENTS: Mapping[ExecutionIntent, CapabilityRequirement] = MappingProxyType({
    ExecutionIntent.STRUCTURED_OUTPUT: CapabilityRequirement(
        floors=(CapabilityFloor(capability=MeasuredCapability.STRUCTURED_JSON),)),
    ExecutionIntent.TEXT_SUMMARY: CapabilityRequirement(
        floors=(CapabilityFloor(capability=MeasuredCapability.SUMMARIZATION),)),
})


@dataclass(frozen=True)
class ExecutionRefusal:
    code: str
    reasons: tuple[str, ...] = ()


class ExecutionBindingRefused(Exception):  # noqa: N818 - a refusal, not an internal error
    def __init__(self, refusal: ExecutionRefusal) -> None:
        super().__init__(refusal.code if not refusal.reasons else f"{refusal.code}:{','.join(refusal.reasons)}")
        self.refusal = refusal


def refuse(code: str, reasons: Iterable[str] = ()) -> ExecutionBindingRefused:
    return ExecutionBindingRefused(ExecutionRefusal(code, tuple(dict.fromkeys(reasons))[:_MAX_REASONS]))


def record_endpoint(url: str) -> str:
    """scheme://host:port only: credentials, paths and queries are never recorded."""
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        port = None
    host = parsed.hostname or "invalid"
    host = f"[{host}]" if ":" in host else host
    scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "invalid"
    return f"{scheme}://{host}" + (f":{port}" if port else "")


@dataclass(frozen=True)
class BoundExecution:
    """One measured profile, live-bound, authorized and admitted for one intent: what a provider may execute."""

    intent: ExecutionIntent
    profile_id: str
    model_ref: str
    manifest_digest: str
    runtime_ref: str
    runtime_version: str
    device_profile_id: str
    binding_hash: str
    requirement_hash: str
    execution: ExecutionConfig
    config_hash: str
    endpoint: str
    timeout_seconds: int
    authorization: str = AUTHORIZATION_SOURCE

    def evidence(self) -> dict[str, Any]:
        """JSON-safe, secret-free record of the binding (endpoint as scheme://host:port only)."""
        return {
            "schema": EVIDENCE_SCHEMA, "intent": self.intent.value, "profile_id": self.profile_id,
            "model_ref": self.model_ref, "manifest_digest": self.manifest_digest, "runtime_ref": self.runtime_ref,
            "runtime_version": self.runtime_version, "device_profile_id": self.device_profile_id,
            "binding_hash": self.binding_hash, "requirement_hash": self.requirement_hash,
            "execution": self.execution.model_dump(mode="json"), "config_hash": self.config_hash,
            "endpoint": record_endpoint(self.endpoint), "authorization": self.authorization,
            "timeout_seconds": self.timeout_seconds,
        }


CatalogLoader = Callable[[Path], ModelCatalog]
LiveFactory = Callable[[Settings], LiveLocalState]
RegistrationsLoader = Callable[[Path], tuple[NodeRegistration, ...]]

_LIVE_STATES: dict[tuple[str, ...], LiveLocalState] = {}
_LIVE_LOCK = threading.Lock()


def _load_catalog(path: Path) -> ModelCatalog:
    return ModelCatalogStore(path).load()


def _live_state(settings: Settings) -> LiveLocalState:
    """One shared observer per configured endpoint set (it re-observes on its own bounded refresh interval)."""
    endpoints = default_runtime_endpoints(settings.ollama_base_url)
    key = tuple(item.model_dump_json() for item in endpoints)
    with _LIVE_LOCK:
        if key not in _LIVE_STATES:
            _LIVE_STATES[key] = LiveLocalState.local(endpoints)
        return _LIVE_STATES[key]


def _load_registrations(path: Path) -> tuple[NodeRegistration, ...]:
    return NodeRegistry.load(path).nodes


def _now() -> datetime:
    return datetime.now(UTC)


def _select_profile(settings: Settings, catalog_loader: CatalogLoader) -> ModelCapabilityProfile:
    path = settings.model_catalog_path.strip()
    if not path:
        raise refuse(MEASURED_PROFILE_STORE_NOT_CONFIGURED)
    try:
        catalog = catalog_loader(Path(path))
    except (CatalogStoreError, OSError, ValueError):
        raise refuse(MODEL_CATALOG_STORE_INVALID) from None
    family = settings.chat_provider.strip().casefold()
    matches = [p for p in catalog.capability_profiles()
               if p.artifact.runtime_family == family and p.artifact.artifact_ref == settings.chat_model]
    if not matches:
        raise refuse(MODEL_PROFILE_NOT_REGISTERED)
    if len(matches) > 1:
        raise refuse(MODEL_PROFILE_AMBIGUOUS, sorted(p.profile_id for p in matches))
    return matches[0]


def _bind(profile: ModelCapabilityProfile, settings: Settings, live_factory: LiveFactory,
          now: Callable[[], datetime]) -> LiveRuntimeBinding:
    device, runtimes = live_factory(settings).observations()
    binding = bind_live(profile, runtimes, device, now=now(), policy=BindingPolicy())
    if not binding.bindable or binding.runtime_ref is None or binding.runtime_version is None \
            or binding.device_profile_id is None:
        raise refuse(LIVE_BINDING_REFUSED, binding.reasons)
    return binding


def _authorize(profile: ModelCapabilityProfile, settings: Settings, intent: ExecutionIntent, project_id: str,
               registrations_loader: RegistrationsLoader) -> None:
    path = settings.compute_nodes_config.strip()
    if not path:
        raise refuse(MODEL_AUTHORIZATION_NOT_CONFIGURED)
    try:
        registrations = registrations_loader(Path(path))
    except (OSError, ValueError):
        raise refuse(MODEL_AUTHORIZATION_STORE_INVALID) from None
    request = InferenceRequest(project_id=project_id, stage_id=_STAGE_ID, capability_id=intent.value,
                               risk_level="LOW", classification=DataClassification.INTERNAL,
                               network_policy="LOCAL_ONLY", grant_hash="")
    try:
        authorized = registration_authorizes(lambda: registrations)(profile, request) is True
    except Exception:  # noqa: BLE001 - an authority that cannot answer authorizes nothing (fail closed)
        authorized = False
    if not authorized:
        raise refuse(MODEL_NOT_AUTHORIZED)


def _measured_condition(profile: ModelCapabilityProfile, requirement: CapabilityRequirement,
                        binding: LiveRuntimeBinding) -> MeasuredExecutionCondition:
    condition, unbound = required_execution(profile, requirement)
    if condition is None:
        code, _, detail = (unbound or MEASURED_EXECUTION_CONFIG_MISMATCH).partition(":")
        raise refuse(code, (detail,) if detail else ())
    assessment = assess(profile, requirement, runtime_state=binding.runtime_state,
                        device_state=binding.device_state, headroom=binding.headroom)
    if not assessment.eligible(requirement):
        raise refuse(MEASURED_INELIGIBLE, (*assessment.reasons, *binding.reasons))
    compatibility, reasons = check_execution(condition, condition.config)
    if compatibility is not ExecutionCompatibility.COMPATIBLE:
        raise refuse(MEASURED_EXECUTION_CONFIG_MISMATCH, reasons)
    return condition


def resolve_bound_execution(settings: Settings, intent: ExecutionIntent, *, project_id: str,
                            catalog_loader: CatalogLoader = _load_catalog,
                            live_factory: LiveFactory = _live_state,
                            registrations_loader: RegistrationsLoader = _load_registrations,
                            now: Callable[[], datetime] = _now) -> BoundExecution:
    """The measured execution the configured chat model may run for ``intent``; raises ``ExecutionBindingRefused``.

    The injection points exist for tests only; production uses the catalog store, the local live observer and
    the compute-node registry named by settings."""
    if not isinstance(intent, ExecutionIntent) or not project_id:
        raise refuse(MEASURED_EXECUTION_INTENT_REQUIRED)
    profile = _select_profile(settings, catalog_loader)
    binding = _bind(profile, settings, live_factory, now)
    _authorize(profile, settings, intent, project_id, registrations_loader)
    requirement = INTENT_REQUIREMENTS[intent]
    condition = _measured_condition(profile, requirement, binding)
    assert binding.runtime_ref is not None and binding.runtime_version is not None  # checked in _bind
    assert binding.device_profile_id is not None
    return BoundExecution(
        intent=intent, profile_id=profile.profile_id, model_ref=profile.artifact.model_ref,
        manifest_digest=profile.artifact.manifest_digest, runtime_ref=binding.runtime_ref,
        runtime_version=binding.runtime_version, device_profile_id=binding.device_profile_id,
        binding_hash=binding.binding_hash, requirement_hash=requirement.requirement_hash,
        execution=condition.config, config_hash=condition.config.config_hash, endpoint=settings.ollama_base_url,
        timeout_seconds=settings.local_inference_timeout_seconds)
