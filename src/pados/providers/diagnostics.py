# SPDX-License-Identifier: AGPL-3.0-only
"""Provider list and diagnostics for the Control Center (read and observe only; never authority).

For each provider (the owner's setup entries, plus the Ollama endpoint PADOS settings point at) this reports two
separate levels, which must never be merged:

1. Reachability: health and model discovery through the EXISTING paths (``describe_runtime`` for local runtimes,
   ``GatewayBackend`` for a gateway), model identity (the digest the runtime reports), circuit state, the last
   failure code with a plain-language explanation.
2. Governance per discovered model: whether a REGISTERED measured profile in the operator ``ModelCatalog`` matches
   its exact digest, whether that profile live-binds here (``bind_live``: measured runtime version and device), and
   whether a compute-node registration authorizes its immutable ``model_ref``. For the configured chat model the
   real consumer resolver (``resolve_bound_execution``) is asked as well.

A reachable model is not usable for governed work: without a measured profile it can only produce explicitly
labelled non-authoritative observations, and nothing it says reaches the CAS. No secret value is read into a view:
only the NAME of an auth environment variable and whether it is set.
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pados.capability.inference import registration_authorizes
from pados.capability.seams import InferenceRequest
from pados.compute_node.circuit import circuit_snapshot
from pados.compute_node.gateway import EXPLANATIONS, AttemptRecorder, GatewayBackend, GatewayError, explain
from pados.compute_node.gateway_contract import ContractError, GatewayContract, load_gateway_contract
from pados.compute_node.manifest import NodeCapability
from pados.compute_node.registry import NodeRegistration, NodeRegistry
from pados.config import Settings
from pados.enterprise.classification import DataClassification
from pados.local_intelligence.benchmark import normalize_digest
from pados.local_intelligence.catalog_store import CatalogStoreError, ModelCatalogStore
from pados.local_intelligence.device import DeviceProfile, DeviceProfiler
from pados.local_intelligence.live import LiveLocalState, device_observation, runtime_observation
from pados.local_intelligence.runtimes import (
    RuntimeDescriptor,
    RuntimeEndpoint,
    RuntimeHealth,
    describe_runtime,
)
from pados.providers.execution_binding import (
    ExecutionBindingRefused,
    ExecutionIntent,
    record_endpoint,
    resolve_bound_execution,
)
from pados.providers.setup import GATEWAY_KIND, ProviderSettings, ProvidersSetup, ProvidersSetupStore, SetupError
from pados.resources.capability_profile import BindingPolicy, DeviceObservation, ModelCapabilityProfile, bind_live

VIEW_SCHEMA = "pados.providers.view.v1"
DETECTED_ID = "ollama-local"
PROBE_PROMPT = "PADOS connection probe. Reply with the single word OK."
LOCAL_EXPLANATIONS = {
    "RUNTIME_UNREACHABLE": "The local runtime did not answer. Is it running on this computer?",
    "RUNTIME_PROBE_FAILED": "The runtime answered, but not in the form PADOS expects for this kind.",
    "DISCOVERY_LOOPBACK_ONLY": "PADOS only probes runtimes on this computer (loopback); this endpoint was not probed.",
    "RUNTIME_TYPE_NOT_DISCOVERABLE": "PADOS has no discovery probe for this runtime kind yet.",
    "PROVIDER_DISABLED": "The owner disabled this provider.",
}
GOVERNANCE_EXPLANATIONS = {
    "AUTHORIZED_AND_MEASURED": "A measured profile matches this exact model, binds to this runtime and device, and a "
                               "compute-node registration authorizes it.",
    "MEASURED_NOT_AUTHORIZED": "Measured and bindable here, but no compute-node registration authorizes it.",
    "MEASURED_NOT_APPLICABLE": "A measured profile exists, but its evidence does not apply to this runtime or device.",
    "NOT_MEASURED": "No measured profile for this exact model: it can only give non-authoritative observations.",
}

Describe = Callable[[RuntimeEndpoint, datetime], RuntimeDescriptor]
Profiler = Callable[[], DeviceProfile]


def _now() -> datetime:
    return datetime.now(UTC)


def detected_default(settings: Settings) -> ProviderSettings:
    """The Ollama endpoint PADOS settings already point at (OLLAMA_BASE_URL), shown even before any setup entry."""
    return ProviderSettings(provider_id=DETECTED_ID, kind="ollama", display_name="Ollama (PADOS settings)", enabled=True,
                            endpoint=settings.ollama_base_url, timeout_seconds=settings.local_inference_timeout_seconds,
                            project_ids=("*",), max_classification=DataClassification.INTERNAL)


def env_present(name: str | None) -> bool | None:
    return None if name is None else bool(os.environ.get(name))


class ProviderDiagnostics:
    """Process-wide diagnostics state (last result per provider, gateway attempt recorders)."""

    def __init__(self, settings: Settings, *, describe: Describe | None = None, profiler: Profiler | None = None,
                 clock: Callable[[], datetime] = _now) -> None:
        self.settings = settings
        self.store = ProvidersSetupStore(settings.providers_setup_path)
        self._describe: Describe = describe or (lambda target, now: describe_runtime(target, now=now))
        self._profiler: Profiler = profiler or (lambda: DeviceProfiler.local().profile())
        self._clock = clock
        self._last: dict[str, dict[str, Any]] = {}
        self._recorders: dict[str, AttemptRecorder] = {}
        self._lock = threading.Lock()

    # -- configuration --------------------------------------------------------------------------------------------
    def providers(self, setup: ProvidersSetup) -> list[tuple[ProviderSettings, str]]:
        entries = [(item, "setup") for item in setup.providers]
        if setup.get(DETECTED_ID) is None:
            try:
                entries.insert(0, (detected_default(self.settings), "settings"))
            except ValueError:  # OLLAMA_BASE_URL is not a private endpoint: nothing is detected
                pass
        return entries

    def catalog(self) -> tuple[str, list[ModelCapabilityProfile]]:
        path = self.settings.model_catalog_path.strip()
        if not path:
            return "NOT_CONFIGURED", []
        try:
            return "OK", list(ModelCatalogStore(Path(path)).load().capability_profiles())
        except (CatalogStoreError, OSError, ValueError):
            return "INVALID", []

    def registrations(self) -> tuple[str, tuple[NodeRegistration, ...]]:
        path = self.settings.compute_nodes_config.strip()
        if not path:
            return "NOT_CONFIGURED", ()
        try:
            return "OK", NodeRegistry.load(path).nodes
        except (OSError, ValueError):
            return "INVALID", ()

    def gateway(self, item: ProviderSettings) -> tuple[GatewayBackend | None, str | None]:
        """The gateway backend under the NARROWER of contract and setup policy; (None, code) when unloadable."""
        assert item.contract_path is not None
        try:
            contract = load_gateway_contract(item.contract_path)
        except ContractError as exc:
            return None, str(exc)
        ceiling = item.max_classification if contract.policy.max_classification.permits(item.max_classification) \
            else contract.policy.max_classification
        policy = contract.policy.model_copy(update={
            "max_classification": ceiling, "execution_enabled": contract.policy.execution_enabled and item.enabled})
        timeouts = contract.timeouts.model_copy(update={
            "read_seconds": min(contract.timeouts.read_seconds, float(item.timeout_seconds))})
        # keyed by the setup id, so circuit, receipts and the view agree
        narrowed: GatewayContract = contract.model_copy(update={"policy": policy, "timeouts": timeouts,
                                                                "provider_id": item.provider_id})
        with self._lock:
            recorder = self._recorders.setdefault(item.provider_id, AttemptRecorder(
                Path(self.settings.providers_setup_path).parent / "gateway-attempts"))
        return GatewayBackend(item.provider_id, contract=narrowed, recorder=recorder), None

    # -- views ----------------------------------------------------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        setup = self.store.load()
        catalog_status, profiles = self.catalog()
        registration_status, registrations = self.registrations()
        return {
            "schema": VIEW_SCHEMA, "setup_revision": setup.revision,
            "providers": [self.view(item, source) for item, source in self.providers(setup)],
            "catalog": {"status": catalog_status, "profiles": [_profile_view(p) for p in profiles]},
            "authorizations": {"status": registration_status,
                               "registrations": [_registration_view(r) for r in registrations]},
            "governed_use": "A reachable model is not automatically usable for governed work. Only a measured, "
                            "live-bound and authorized model may run governed steps, and its output is never accepted "
                            "automatically.",
        }

    def view(self, item: ProviderSettings, source: str) -> dict[str, Any]:
        last = self._last.get(item.provider_id)
        settings = item.model_dump(mode="json")
        view: dict[str, Any] = {
            "provider_id": item.provider_id, "kind": item.kind, "display_name": item.display_name or item.provider_id,
            "source": source, "enabled": item.enabled, "settings": settings,
            "auth_env": item.auth_env, "auth_env_present": env_present(item.auth_env),
            # a disabled provider is shown as disabled, whatever an earlier diagnostics run observed
            "reachability": {"state": "DISABLED", "code": "PROVIDER_DISABLED",
                             "explanation": LOCAL_EXPLANATIONS["PROVIDER_DISABLED"]} if not item.enabled
            else (last or {}).get("reachability") or {"state": "NOT_CHECKED", "code": None, "explanation": None},
            "models": (last or {}).get("models", []), "checks": (last or {}).get("checks", []),
            "checked_at": (last or {}).get("checked_at"), "circuit": circuit_snapshot(item.provider_id),
            "recent_attempts": self._recorders[item.provider_id].recent(10) if item.provider_id in self._recorders
            else [],
        }
        if item.kind == GATEWAY_KIND:
            view["gateway"] = self._gateway_summary(item)
        return view

    def _gateway_summary(self, item: ProviderSettings) -> dict[str, Any]:
        assert item.contract_path is not None
        try:
            contract = load_gateway_contract(item.contract_path)
        except ContractError as exc:
            return {"contract": "INVALID", "code": str(exc)}
        env = contract.auth.auth_ref.removeprefix("secretref://env/") if contract.auth.auth_ref else None
        return {"contract": "OK", "base_url": record_endpoint(contract.base_url), "auth_scheme": contract.auth.scheme,
                "auth_env": env, "auth_env_present": env_present(env),
                "contract_max_classification": contract.policy.max_classification.value,
                "contract_execution_enabled": contract.policy.execution_enabled,
                "idempotency_honoured": contract.idempotency.honoured,
                "max_context_tokens": contract.max_context_tokens}

    # -- diagnostics ----------------------------------------------------------------------------------------------
    def diagnose(self, provider_id: str, *, probe_chat: bool = False, model: str | None = None) -> dict[str, Any]:
        setup = self.store.load()
        found = next(((item, source) for item, source in self.providers(setup) if item.provider_id == provider_id),
                     None)
        if found is None:
            raise SetupError("PROVIDER_NOT_CONFIGURED", 404)
        item, source = found
        now = self._clock()
        if not item.enabled:
            result: dict[str, Any] = {"reachability": {"state": "DISABLED", "code": "PROVIDER_DISABLED",
                                                       "explanation": LOCAL_EXPLANATIONS["PROVIDER_DISABLED"]},
                                      "models": [], "checks": []}
        elif item.kind == GATEWAY_KIND:
            result = self._diagnose_gateway(item, now, probe_chat=probe_chat, model=model)
        else:
            result = self._diagnose_local(item, now)
        result["checked_at"] = now.isoformat()
        self._last[item.provider_id] = result
        return self.view(item, source)

    def _diagnose_local(self, item: ProviderSettings, now: datetime) -> dict[str, Any]:
        assert item.endpoint is not None
        runtime_id = item.provider_id if item.provider_id[0].isalpha() else f"p-{item.provider_id}"
        descriptor = self._describe(RuntimeEndpoint(runtime_id=runtime_id, runtime_type=item.kind,
                                                    endpoint=item.endpoint), now)
        state = {RuntimeHealth.HEALTHY: "REACHABLE", RuntimeHealth.UNREACHABLE: "UNREACHABLE",
                 RuntimeHealth.PROBE_FAILED: "DEGRADED"}.get(descriptor.health, "NOT_CHECKED")
        code = descriptor.failure_code
        explanation = _explain(code) or LOCAL_EXPLANATIONS.get(f"RUNTIME_{descriptor.health.value}")
        checks = [_check("health", "PASS" if state == "REACHABLE" else "FAIL", code, explanation),
                  _check("discovery", "PASS" if descriptor.models else "FAIL" if state == "REACHABLE" else "SKIPPED",
                         None, f"{len(descriptor.models)} models reported" if descriptor.models else None)]
        if state != "REACHABLE":
            return {"reachability": {"state": state, "code": code, "explanation": explanation,
                                     "runtime_version": descriptor.version}, "models": [], "checks": checks}
        raw_device: DeviceProfile | None = None
        try:
            raw_device = self._profiler()
            device: DeviceObservation | None = device_observation(raw_device)
        except Exception:  # noqa: BLE001 - an unreadable device is reported, never guessed
            device = None
        runtime = runtime_observation(descriptor)
        loaded = {m.name for m in descriptor.loaded_models}
        models = [self._governance(item.kind, m.name, m.digest, m.name in loaded, (runtime,), device, now)
                  for m in descriptor.models]
        identity = all(m["digest"] for m in models)
        checks.append(_check("model_identity", "PASS" if identity else "FAIL", None if identity else
                             "MODEL_DIGEST_NOT_REPORTED", "Every model reports a content digest." if identity else
                             "Some models report no digest; they can never match a measured profile."))
        checks.extend(self._chat_binding(item, descriptor, raw_device))
        return {"reachability": {"state": "REACHABLE", "code": None, "explanation": None,
                                 "runtime_version": descriptor.version}, "models": models, "checks": checks}

    def _chat_binding(self, item: ProviderSettings, descriptor: RuntimeDescriptor,
                      raw_device: DeviceProfile | None) -> list[dict[str, Any]]:
        """The real consumer resolver for the configured chat model (only for the endpoint the settings use)."""
        settings = self.settings
        if settings.chat_provider.strip().casefold() != item.kind or not settings.chat_model \
                or item.endpoint is None or record_endpoint(item.endpoint) != record_endpoint(settings.ollama_base_url):
            return []
        intent = ExecutionIntent(item.capability_requirement or ExecutionIntent.STRUCTURED_OUTPUT.value)
        project = next((p for p in item.project_ids if p != "*"), "*")
        live = LiveLocalState(profiler=lambda: _required(raw_device), endpoints=(RuntimeEndpoint(
            runtime_id=descriptor.runtime_id, runtime_type=item.kind, endpoint=settings.ollama_base_url),),
            describe=lambda target, now: descriptor)
        try:
            bound = resolve_bound_execution(settings, intent, project_id=project, live_factory=lambda _: live,
                                            now=self._clock)
        except ExecutionBindingRefused as exc:
            return [_check("governed_binding", "REFUSED", exc.refusal.code,
                           "The configured chat model may not run governed work here: " + exc.refusal.code,
                           reasons=list(exc.refusal.reasons), model=settings.chat_model, intent=intent.value)]
        return [_check("governed_binding", "BOUND", None, "The configured chat model is measured, live-bound and "
                       "authorized for this intent.", model=settings.chat_model, intent=intent.value,
                       profile_id=bound.profile_id, binding=bound.evidence())]

    def _governance(self, kind: str, name: str, digest: str | None, loaded: bool | None,
                    runtimes: tuple[Any, ...], device: DeviceObservation | None, now: datetime) -> dict[str, Any]:
        _, profiles = self.catalog()
        _, registrations = self.registrations()
        normalized = normalize_digest(digest)
        matching = [p for p in profiles if p.artifact.runtime_family == kind and normalized is not None
                    and p.artifact.manifest_digest == normalized]
        same_tag = [p.profile_id for p in profiles if p.artifact.runtime_family == kind
                    and p.artifact.artifact_ref == name and p not in matching]
        row: dict[str, Any] = {"model": name, "digest": normalized or digest, "loaded": loaded,
                               "governance": "NOT_MEASURED", "profile_id": None, "reasons": [],
                               "authorized_by": [], "authority": "NON_AUTHORITATIVE_OBSERVATION"}
        if same_tag:
            row["reasons"] = ["MEASURED_ARTIFACT_DIGEST_DIFFERS"]
        for profile in matching:
            binding = bind_live(profile, runtimes, device, now=now, policy=BindingPolicy())
            authorizes = registration_authorizes(lambda: registrations)
            request = InferenceRequest(project_id="*", stage_id="provider-diagnostics", capability_id="DIAGNOSTICS",
                                       risk_level="LOW", classification=DataClassification.INTERNAL,
                                       network_policy="LOCAL_ONLY", grant_hash="")
            by = [r.node_id for r in registrations if r.authorized and NodeCapability.LOCAL_INFERENCE in
                  r.allowed_capabilities and profile.artifact.model_ref in r.allowed_models]
            status = ("AUTHORIZED_AND_MEASURED" if binding.bindable and by else "MEASURED_NOT_AUTHORIZED"
                      if binding.bindable else "MEASURED_NOT_APPLICABLE")
            row.update({"governance": status, "profile_id": profile.profile_id, "model_ref": profile.artifact.model_ref,
                        "measured_runtime_version": profile.binding.runtime_version,
                        "measured_device_profile_id": profile.binding.device_profile_id,
                        "reasons": list(binding.reasons), "authorized_by": by,
                        "authorized_for_all_projects": bool(authorizes(profile, request)),
                        "authority": "MEASURED_PROFILE" if status == "AUTHORIZED_AND_MEASURED" else row["authority"]})
            if status == "AUTHORIZED_AND_MEASURED":
                break
        row["explanation"] = GOVERNANCE_EXPLANATIONS[row["governance"]]
        return row

    def _diagnose_gateway(self, item: ProviderSettings, now: datetime, *, probe_chat: bool,
                          model: str | None) -> dict[str, Any]:
        backend, code = self.gateway(item)
        if backend is None:
            return {"reachability": {"state": "NOT_CHECKED", "code": code, "explanation":
                                     "The private gateway contract could not be loaded."}, "models": [],
                    "checks": [_check("contract", "FAIL", code, "The private gateway contract could not be loaded.")]}
        checks = [_check("contract", "PASS", None, "The private gateway contract is valid.")]
        try:
            backend.check_health(probe=True)
            checks.append(_check("health", "PASS", None, None))
        except GatewayError as exc:
            checks.append(_check("health", "FAIL", exc.code, explain(exc.code)))
            return {"reachability": {"state": "UNREACHABLE", "code": exc.code, "explanation": explain(exc.code)},
                    "models": [], "checks": checks}
        try:
            found = backend.discover_detailed()
            checks.append(_check("discovery", "PASS", None, f"{len(found)} models reported"))
        except GatewayError as exc:
            checks.append(_check("discovery", "FAIL", exc.code, explain(exc.code)))
            found = []
        models = [self._governance(GATEWAY_KIND, m["model"], m["digest"], m["loaded"], (), None, now) for m in found]
        if probe_chat:
            checks.append(self._probe_chat(backend, model or next((m["model"] for m in found if m["loaded"]), None)))
        return {"reachability": {"state": "REACHABLE", "code": None, "explanation": None}, "models": models,
                "checks": checks}

    @staticmethod
    def _probe_chat(backend: GatewayBackend, model: str | None) -> dict[str, Any]:
        """One fixed, non-sensitive PUBLIC prompt; only a hash and counters come back (never the text)."""
        if model is None:
            return _check("probe_chat", "SKIPPED", "GATEWAY_MODEL_NOT_LOADED", explain("GATEWAY_MODEL_NOT_LOADED"))
        try:
            output = backend.run(NodeCapability.LOCAL_INFERENCE, model,
                                 {"messages": [{"role": "user", "content": PROBE_PROMPT}]}, max_output_tokens=16,
                                 timeout_seconds=backend.contract.timeouts.read_seconds,
                                 classification=DataClassification.PUBLIC)
        except GatewayError as exc:
            return _check("probe_chat", "FAIL", exc.code, explain(exc.code), model=model)
        except Exception as exc:  # noqa: BLE001 - a backend refusal code only (never provider text)
            code = getattr(exc, "code", type(exc).__name__)
            return _check("probe_chat", "FAIL", str(code)[:80], None, model=model)
        return _check("probe_chat", "PASS", None, "The gateway answered the fixed probe. This is a non-authoritative "
                      "observation, not a measurement.", model=model, authority=output.runtime["authority"],
                      answer_chars=len(output.content),
                      answer_sha256=hashlib.sha256(output.content.encode("utf-8")).hexdigest(),
                      usage=output.runtime.get("usage"))


def _required(profile: DeviceProfile | None) -> DeviceProfile:
    if profile is None:
        raise RuntimeError("DEVICE_PROFILE_UNAVAILABLE")
    return profile


def _explain(code: str | None) -> str | None:
    if code is None:
        return None
    return LOCAL_EXPLANATIONS.get(code) or EXPLANATIONS.get(code) or LOCAL_EXPLANATIONS.get(code.split(":", 1)[0])


def _check(name: str, status: str, code: str | None, explanation: str | None, **extra: Any) -> dict[str, Any]:
    return {"check": name, "status": status, "code": code, "explanation": explanation, **extra}


def _profile_view(profile: ModelCapabilityProfile) -> dict[str, Any]:
    return {"profile_id": profile.profile_id, "runtime_family": profile.artifact.runtime_family,
            "artifact_ref": profile.artifact.artifact_ref, "manifest_digest": profile.artifact.manifest_digest,
            "model_ref": profile.artifact.model_ref, "runtime_version": profile.binding.runtime_version,
            "device_profile_id": profile.binding.device_profile_id, "lifecycle": "REGISTERED"}


def _registration_view(item: NodeRegistration) -> dict[str, Any]:
    return {"node_id": item.node_id, "node_name": item.node_name, "authorized": item.authorized,
            "allowed_capabilities": [c.value for c in item.allowed_capabilities],
            "max_classification": item.max_classification.value, "project_ids": list(item.project_ids),
            "allowed_models": list(item.allowed_models)}
