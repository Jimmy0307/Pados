"""Stage C composition: the M4.1 governed chain over SEVERAL measured profiles on SEVERAL runtimes (tests only).

Nothing here is new authority. It composes exactly the M4.1 pieces (``m4_1_live_chain.LiveChain``: ModelCatalog
store, LiveLocalState, WorkerRouterBridge, planner, executor, LocalInferenceStageAdapter, LocalInferenceProvider
with its MeasuredExecutionGate, the real node app) and only generalizes the wiring that ``build_chain`` fixes to one
profile: one runtime endpoint per runtime family, one node backend per family and one node model per registered
profile. Every runtime kind, artifact name and digest comes from the persisted profiles and the runtime specs (data).
"""
from __future__ import annotations

import secrets
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "stage_b"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from capability_profile_support import counted, make_profile, observations  # noqa: E402
from live_chain_support import device_id  # noqa: E402
from m4_1_live_chain import AUTH_REF, CAPABILITY, CREDENTIAL_ENV, FileGrants, LiveChain  # noqa: E402

from pados.compute_node.artifact_identity import LocalArtifact, LocalArtifactIdentity  # noqa: E402
from pados.compute_node.config import NodeConfig  # noqa: E402
from pados.compute_node.service import NodeRuntime, create_node_app  # noqa: E402
from pados.enterprise.secrets import EnvironmentSecretResolver  # noqa: E402
from pados.local_intelligence.catalog_store import ModelCatalogStore  # noqa: E402
from pados.local_intelligence.live import LiveLocalState  # noqa: E402
from pados.local_intelligence.runtimes import RuntimeEndpoint  # noqa: E402
from pados.resources.capability_profile import (  # noqa: E402
    BindingPolicy,
    CapabilityObservation,
    MeasuredCapability,
    ModelCapabilityProfile,
    profile_id_for,
)

LLAMA_FAMILY = "llama_cpp_native"  # test DATA (the second runtime family label); generic code never names it
LLAMA_VERSION = "6123+1a2b3c4"  # what the fake's build_info b6123-1a2b3c4 normalizes to


def gguf_file(root: Path, name: str, seed: str) -> Path:
    """A small file with the GGUF magic (the identity is over the bytes; nothing parses the weights)."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_bytes(b"GGUF" + (seed.encode("utf-8") * 4096))
    return path


def identity_of(root: Path, path: Path) -> LocalArtifact:
    artifact = LocalArtifactIdentity(root).identify(str(path))
    assert artifact is not None
    return artifact


def llama_profile(root: Path, path: Path, *, alias: str, model_family: str = "brand-b",
                  runtime_version: str = LLAMA_VERSION, obs: tuple[CapabilityObservation, ...] | None = None,
                  **extra: Any) -> ModelCapabilityProfile:
    """A measured profile of the artifact at ``path`` on the second runtime family (synthetic evidence)."""
    artifact = identity_of(root, path)
    found = obs if obs is not None else observations(
        extract=counted(MeasuredCapability.INFORMATION_EXTRACTION, 2, 2, lower=0.3424))
    profile = make_profile(artifact_ref=alias, runtime_family=LLAMA_FAMILY, runtime_version=runtime_version,
                           model_family=model_family, device_id=device_id(), obs=found, **extra)
    body = profile.model_dump(mode="json", exclude={"profile_id"})
    body["artifact"]["manifest_digest"] = artifact.manifest_digest
    body["artifact"]["layer_digests"] = [artifact.layer_digest]
    return ModelCapabilityProfile.model_validate({**body, "profile_id": profile_id_for(body)})


@dataclass(frozen=True)
class RuntimeSpec:
    family: str  # the compute-node backend kind (data, from the profile the runtime serves)
    endpoint: str
    artifact_root: str | None = None


def build_multi_runtime_chain(workdir: Path, *, catalog_path: Path, runtimes: Sequence[RuntimeSpec],
                              profiler: Callable[[], Any], clock: Callable[[], datetime],
                              policy: BindingPolicy | None = None) -> tuple[LiveChain, Callable[[], None]]:
    from fastapi.testclient import TestClient

    workdir.mkdir(parents=True, exist_ok=True)
    store = ModelCatalogStore(catalog_path)
    profiles = store.load().capability_profiles()
    if not profiles:
        raise RuntimeError("STAGE_C_EXPECTS_REGISTERED_PROFILES")
    endpoints = tuple(RuntimeEndpoint(runtime_id=f"rt-{spec.family.replace('_', '-')}", runtime_type=spec.family,
                                      endpoint=spec.endpoint, artifact_root=spec.artifact_root) for spec in runtimes)
    live = LiveLocalState(profiler=profiler, endpoints=endpoints, refresh_seconds=0.0, clock=clock)
    policy = policy or BindingPolicy()
    bound = live.bindings(profiles, policy)
    gpu = any(p.footprint.gpu_memory_mib for p in profiles)
    backends = [{"backend_id": spec.family, "kind": spec.family, "endpoint": spec.endpoint,
                 **({"artifact_root": spec.artifact_root} if spec.artifact_root else {})} for spec in runtimes]
    models = [{"backend_id": p.binding.runtime_family, "backend_model": b.runtime_ref or p.artifact.artifact_ref,
               "family": p.artifact.model_family, "version": p.artifact.manifest_digest,
               "digest": p.artifact.manifest_digest, "quantization": p.artifact.quantization,
               "context_limit_tokens": p.footprint.base_context_tokens, "tasks": ["LOCAL_INFERENCE"],
               "resource_class": "GPU" if gpu else "CPU_LIGHT", "privacy_class": "INTERNAL"} for p, b in bound]
    credential = secrets.token_hex(24)
    config = NodeConfig.model_validate({
        "node_name": "stage-c-node", "state_dir": workdir / "node-state", "credential_ref": AUTH_REF, "port": 8765,
        "privacy_class": "INTERNAL", "resource_class": "GPU" if gpu else "CPU_LIGHT",
        "capabilities": ["LOCAL_INFERENCE"],
        "limits": {"max_concurrent_jobs": 1, "max_queue_depth": 1, "default_timeout_seconds": 180,
                   "max_timeout_seconds": 300, "max_output_tokens": 512},
        "backends": backends, "models": models})
    runtime = NodeRuntime(config, resolver=EnvironmentSecretResolver({CREDENTIAL_ENV: credential}))
    app = create_node_app(runtime)

    def http(_: str) -> Any:
        return TestClient(app, base_url=config.endpoint)

    grants = FileGrants(workdir / "grant.json")
    grants.write(CAPABILITY)
    chain = LiveChain(workdir=workdir, profile=profiles[0], catalog_store=store, live=live, policy=policy,
                      registry_path=workdir / "registry.json", grants=grants, node_runtime=runtime,
                      node_endpoint=config.endpoint, credential=credential, http=http)
    return chain, runtime.jobs.shutdown
