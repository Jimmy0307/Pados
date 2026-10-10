"""RC2 D2: the consumer inference path runs a local model only under a measured execution binding.

Real components: ModelCatalogStore, LiveLocalState over the device profiler and runtime discovery, bind_live,
registration_authorizes over a compute-node registry file, required_execution / assess / check_execution, and the
compute-node OllamaBackend over HTTP. The device is a fake host and the runtime is the loopback FakeOllama: never
model evidence, never the real runtime.
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources"))
from capability_profile_support import measured_config  # noqa: E402
from fake_ollama import FakeOllama  # noqa: E402
from live_chain_support import Clock, fake_profiler, persist, rehearsal_profile  # noqa: E402

from pados.compute_node.backends import BackendError, BackendOutput, DiscoveredModel  # noqa: E402
from pados.compute_node.manifest import NodeCapability  # noqa: E402
from pados.compute_node.registry import NodeRegistration  # noqa: E402
from pados.config import Settings  # noqa: E402
from pados.enterprise.classification import DataClassification  # noqa: E402
from pados.graph.cognition import COGNITIVE_RESPONSE_SCHEMA  # noqa: E402
from pados.local_intelligence.live import LiveLocalState  # noqa: E402
from pados.local_intelligence.runtimes import default_runtime_endpoints  # noqa: E402
from pados.providers.execution_binding import (  # noqa: E402
    BoundExecution,
    ExecutionBindingRefused,
    ExecutionIntent,
    resolve_bound_execution,
)
from pados.providers.factory import ProviderNotConfiguredError, build_chat_provider  # noqa: E402
from pados.providers.mock import MockProvider  # noqa: E402
from pados.providers.ollama import (  # noqa: E402
    MeasuredChatProvider,
    MeasuredExecutionFailed,
    MeasuredExecutionTimeout,
)
from pados.resources.capability_profile import (  # noqa: E402
    ExecutionConfig,
    MeasuredCapability,
    MeasuredExecutionCondition,
    ModelCapabilityProfile,
)

PROJECT = "11111111-2222-4333-8444-555555555555"
JSON_CAP = MeasuredCapability.STRUCTURED_JSON
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


@dataclass
class World:
    tmp: Path
    fake: FakeOllama
    clock: Clock
    profile: ModelCapabilityProfile
    vram_free: int | None = None

    @property
    def catalog(self) -> Path:
        return self.tmp / "catalog.json"

    @property
    def registry(self) -> Path:
        return self.tmp / "compute-nodes.json"

    def settings(self, **overrides: object) -> Settings:
        values: dict[str, object] = {
            "PADOS_ENV": "test", "PADOS_CHAT_PROVIDER": self.profile.artifact.runtime_family,
            "PADOS_CHAT_MODEL": self.profile.artifact.artifact_ref, "OLLAMA_BASE_URL": self.fake.url,
            "PADOS_MODEL_CATALOG": str(self.catalog), "PADOS_COMPUTE_NODES_CONFIG": str(self.registry)}
        values.update(overrides)
        return Settings.model_validate(values)

    def register(self, *, authorized: bool = True, models: tuple[str, ...] | None = None) -> None:
        registration = NodeRegistration(
            node_id="lcn-" + "0" * 20, endpoint=self.fake.url, auth_ref="secretref://env/PADOS_TEST_NODE_KEY",
            authorized=authorized, allowed_capabilities=(NodeCapability.LOCAL_INFERENCE,),
            max_classification=DataClassification.INTERNAL, project_ids=(PROJECT,),
            allowed_models=(self.profile.artifact.model_ref,) if models is None else models,
            security_profile="LOCAL_SINGLE_USER")  # type: ignore[arg-type]
        document = {"schema_version": "pados.compute-nodes.registry.v1", "state_dir": str(self.tmp / "state"),
                    "nodes": [registration.model_dump(mode="json")]}
        self.registry.write_text(json.dumps(document), encoding="utf-8")

    def live(self, settings: Settings) -> LiveLocalState:
        return LiveLocalState(profiler=fake_profiler(self.clock, vram_free=self.vram_free),
                              endpoints=default_runtime_endpoints(settings.ollama_base_url), clock=self.clock)

    def resolve(self, intent: ExecutionIntent = ExecutionIntent.STRUCTURED_OUTPUT, **overrides: object) -> BoundExecution:
        return resolve_bound_execution(self.settings(**overrides), intent, project_id=PROJECT,
                                       live_factory=self.live, now=lambda: self.clock.now)

    def refusal(self, intent: ExecutionIntent = ExecutionIntent.STRUCTURED_OUTPUT,
                **overrides: object) -> tuple[str, tuple[str, ...]]:
        with pytest.raises(ExecutionBindingRefused) as refused:
            self.resolve(intent, **overrides)
        return refused.value.refusal.code, refused.value.refusal.reasons

    def requests(self, path: str) -> list[dict[str, Any]]:
        return [item["body"] for item in self.fake.requests if item["path"] == path]


def make_world(tmp: Path, *, profiles: tuple[ModelCapabilityProfile, ...] | None = None, register: bool = True,
               vram_free: int | None = None, **fake: Any) -> Iterator[World]:
    chosen = profiles or (rehearsal_profile(),)
    persist(tmp / "catalog.json", *chosen)
    with FakeOllama(present=True, **fake) as runtime:
        world = World(tmp, runtime, Clock(), chosen[0], vram_free)
        if register:
            world.register()
        yield world


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    yield from make_world(tmp_path)


def with_conditions(*conditions: MeasuredExecutionCondition) -> ModelCapabilityProfile:
    return rehearsal_profile(execution_conditions=conditions)


def json_condition(**config: Any) -> MeasuredExecutionCondition:
    observed = config.pop("observed", 70)
    return MeasuredExecutionCondition(capabilities=(JSON_CAP,), config=measured_config(JSON_CAP, **config),
                                      max_observed_output_tokens=observed, sample_size=1, evidence_refs=("task",))


# --- success path ---------------------------------------------------------------------------------------------

def test_bound_call_reproduces_the_measured_condition_and_records_evidence(world: World) -> None:
    bound = world.resolve()
    measured = world.profile.execution_conditions
    condition = next(item for item in measured if JSON_CAP in item.capabilities).config
    assert bound.execution == condition and bound.timeout_seconds == 30
    provider = MeasuredChatProvider(bound)
    content = asyncio.run(provider.chat("Extract INV-88213 as JSON.", response_schema=COGNITIVE_RESPONSE_SCHEMA))
    assert json.loads(content)["invoice_number"] == "INV-88213"
    (body,) = world.requests("/api/chat")
    assert body == {
        "model": world.profile.artifact.artifact_ref, "stream": False, "think": False,
        "messages": [{"role": "user", "content": "Extract INV-88213 as JSON."}],
        "options": {"num_ctx": condition.context_window, "num_predict": condition.max_output_tokens,
                    "temperature": condition.temperature, "seed": condition.seed},
        "format": COGNITIVE_RESPONSE_SCHEMA}
    assert world.requests("/api/generate") == []  # the unbound request shape is never sent
    evidence = provider.last_evidence
    assert evidence is not None
    assert evidence["profile_id"] == world.profile.profile_id
    assert evidence["manifest_digest"] == world.profile.artifact.manifest_digest
    assert evidence["device_profile_id"] == world.profile.device.device_profile_id
    assert evidence["execution"] == condition.model_dump(mode="json")
    assert evidence["config_hash"] == condition.config_hash and evidence["timeout_seconds"] == 30
    assert evidence["authorization"] == "COMPUTE_NODE_REGISTRATION"
    assert evidence["endpoint"] == world.fake.url and evidence["runtime"]["done_reason"] == "stop"
    assert {"eval_count", "prompt_eval_count", "total_duration", "response_schema_sha256"} <= set(evidence["runtime"])
    json.dumps(evidence)  # JSON-safe


def test_timeout_is_a_bounded_setting_recorded_in_evidence(world: World) -> None:
    assert world.settings().local_inference_timeout_seconds == 30
    assert world.resolve(PADOS_LOCAL_INFERENCE_TIMEOUT_SECONDS=90).evidence()["timeout_seconds"] == 90
    for bad in (0, 601):
        with pytest.raises(ValidationError):
            world.settings(PADOS_LOCAL_INFERENCE_TIMEOUT_SECONDS=bad)


def test_text_summary_intent_binds_the_unconstrained_summary_condition(world: World) -> None:
    bound = world.resolve(ExecutionIntent.TEXT_SUMMARY)
    assert bound.execution.output_constraint.value == "UNCONSTRAINED"
    provider = MeasuredChatProvider(bound)
    with pytest.raises(ExecutionBindingRefused, match="MEASURED_EXECUTION_CONFIG_MISMATCH:output_constraint"):
        asyncio.run(provider.chat("summarize", response_schema=SCHEMA))  # a schema needs the structured intent
    assert asyncio.run(provider.chat("admission control in one sentence"))
    assert "format" not in world.requests("/api/chat")[-1]


# --- refusals: store and catalog --------------------------------------------------------------------------------

def test_no_catalog_configured_refuses(world: World) -> None:
    assert world.refusal(PADOS_MODEL_CATALOG="") == ("MEASURED_PROFILE_STORE_NOT_CONFIGURED", ())


def test_invalid_catalog_refuses(world: World) -> None:
    world.catalog.write_text("{not json", encoding="utf-8")
    assert world.refusal()[0] == "MODEL_CATALOG_STORE_INVALID"


def test_tampered_profile_id_is_rejected_by_the_catalog(world: World) -> None:
    document = json.loads(world.catalog.read_text(encoding="utf-8"))
    document["capability_profiles"][0]["profile_id"] = "mcp-" + "0" * 64
    world.catalog.write_text(json.dumps(document), encoding="utf-8")
    assert world.refusal()[0] == "MODEL_CATALOG_STORE_INVALID"


def test_unregistered_model_refuses(world: World) -> None:
    assert world.refusal(PADOS_CHAT_MODEL="another-model:1b")[0] == "MODEL_PROFILE_NOT_REGISTERED"
    assert world.refusal(PADOS_CHAT_PROVIDER="runtime-y")[0] == "MODEL_PROFILE_NOT_REGISTERED"


def test_two_profiles_for_the_same_reference_are_ambiguous(tmp_path: Path) -> None:
    profiles = (rehearsal_profile(), rehearsal_profile(manifest_seed="9"))
    for world in make_world(tmp_path, profiles=profiles):
        code, reasons = world.refusal()
        assert code == "MODEL_PROFILE_AMBIGUOUS" and set(reasons) == {p.profile_id for p in profiles}
        assert world.requests("/api/chat") == []


# --- refusals: live binding ---------------------------------------------------------------------------------------

def test_manifest_digest_not_present_in_the_runtime_refuses(tmp_path: Path) -> None:
    for world in make_world(tmp_path, digest_hex="f" * 64):  # same tag, another manifest
        code, reasons = world.refusal()
        assert code == "LIVE_BINDING_REFUSED" and "MODEL_ARTIFACT_NOT_PRESENT" in reasons


def test_another_device_refuses(tmp_path: Path) -> None:
    for world in make_world(tmp_path, profiles=(rehearsal_profile(device_id="d" * 64),)):
        code, reasons = world.refusal()
        assert code == "LIVE_BINDING_REFUSED" and "DEVICE_NOT_MEASURED" in reasons


def test_unmeasured_runtime_version_refuses(tmp_path: Path) -> None:
    for world in make_world(tmp_path, version="0.99.0"):
        code, reasons = world.refusal()
        assert code == "LIVE_BINDING_REFUSED" and "RUNTIME_VERSION_NOT_MEASURED" in reasons


# --- refusals: authorization ----------------------------------------------------------------------------------------

def test_no_registry_configured_refuses(world: World) -> None:
    assert world.refusal(PADOS_COMPUTE_NODES_CONFIG="")[0] == "MODEL_AUTHORIZATION_NOT_CONFIGURED"


def test_invalid_registry_refuses(world: World) -> None:
    world.registry.write_text("{}", encoding="utf-8")
    assert world.refusal()[0] == "MODEL_AUTHORIZATION_STORE_INVALID"


def test_unauthorized_registration_refuses(world: World) -> None:
    world.register(authorized=False)
    assert world.refusal()[0] == "MODEL_NOT_AUTHORIZED"


def test_revoked_model_reference_refuses(world: World) -> None:
    world.register(models=())  # node stays authorized; the model reference is no longer named
    assert world.refusal()[0] == "MODEL_NOT_AUTHORIZED"
    world.register(models=(world.profile.artifact.model_ref.replace("@sha256:5", "@sha256:6"),))
    assert world.refusal()[0] == "MODEL_NOT_AUTHORIZED"


def test_other_project_is_not_admitted(world: World) -> None:
    with pytest.raises(ExecutionBindingRefused, match="MODEL_NOT_AUTHORIZED"):
        resolve_bound_execution(world.settings(), ExecutionIntent.STRUCTURED_OUTPUT, project_id="other",
                                live_factory=world.live, now=lambda: world.clock.now)


# --- refusals: measured execution -------------------------------------------------------------------------------

def test_inconsistent_measured_conditions_refuse(tmp_path: Path) -> None:
    profile = with_conditions(json_condition(), json_condition(temperature=0.7))
    for world in make_world(tmp_path, profiles=(profile,)):
        assert world.refusal() == ("MEASURED_EXECUTION_CONFIG_INCONSISTENT", ())


def test_missing_measured_condition_refuses(tmp_path: Path) -> None:
    summary = MeasuredExecutionCondition(capabilities=(MeasuredCapability.SUMMARIZATION,),
                                         config=measured_config(MeasuredCapability.SUMMARIZATION),
                                         max_observed_output_tokens=70, sample_size=2)
    for world in make_world(tmp_path, profiles=(with_conditions(summary),)):
        assert world.refusal() == ("MEASURED_EXECUTION_CONFIG_MISSING", ("STRUCTURED_JSON",))


def test_measured_budget_below_the_observed_completion_is_a_config_mismatch(tmp_path: Path) -> None:
    profile = with_conditions(json_condition(max_output_tokens=64, observed=70))
    for world in make_world(tmp_path, profiles=(profile,)):
        assert world.refusal() == ("MEASURED_EXECUTION_CONFIG_MISMATCH",
                                   ("MEASURED_EXECUTION_CONFIG_MISMATCH:max_output_tokens",))


def test_resource_pressure_is_measured_ineligible(tmp_path: Path) -> None:
    for world in make_world(tmp_path, vram_free=100):
        code, reasons = world.refusal()
        assert code == "MEASURED_INELIGIBLE" and "RESOURCE_PRESSURE_PARTIAL_OFFLOAD" in reasons


# --- provider: no unbound call ---------------------------------------------------------------------------------

class ScriptedBackend:
    backend_id = "scripted"
    kind = "scripted"

    def __init__(self, *, echo: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.echo, self.error, self.calls = echo, error, 0

    def health(self) -> bool:
        return True

    def discover(self) -> list[DiscoveredModel]:
        return []

    def run(self, capability: NodeCapability, backend_model: str, item: dict[str, Any], *,
            max_output_tokens: int, timeout_seconds: float, execution: ExecutionConfig | None = None) -> BackendOutput:
        self.calls += 1
        if self.error:
            raise BackendError(self.error, {"done_reason": "length"})
        assert execution is not None
        return BackendOutput('{"answer": "x"}', {"execution_requested": self.echo or execution.model_dump(mode="json")})


def test_structured_binding_never_sends_an_unconstrained_request(world: World) -> None:
    backend = ScriptedBackend()
    provider = MeasuredChatProvider(world.resolve(), backend=backend)
    with pytest.raises(ExecutionBindingRefused, match="MEASURED_EXECUTION_CONFIG_MISMATCH:output_constraint"):
        asyncio.run(provider.chat("no schema"))
    assert backend.calls == 0


def test_configuration_the_backend_did_not_apply_is_refused(world: World) -> None:
    bound = world.resolve()
    echo = bound.execution.model_copy(update={"context_window": 4096}).model_dump(mode="json")
    provider = MeasuredChatProvider(bound, backend=ScriptedBackend(echo=echo))
    with pytest.raises(ExecutionBindingRefused, match="MEASURED_EXECUTION_CONFIG_NOT_APPLIED"):
        asyncio.run(provider.chat("x", response_schema=SCHEMA))
    assert provider.last_evidence is None


def test_backend_timeout_and_failure_are_typed_with_evidence(world: World) -> None:
    bound = world.resolve(PADOS_LOCAL_INFERENCE_TIMEOUT_SECONDS=5)
    timed_out = MeasuredChatProvider(bound, backend=ScriptedBackend(error="BACKEND_TIMEOUT"))
    with pytest.raises(MeasuredExecutionTimeout):
        asyncio.run(timed_out.chat("x", response_schema=SCHEMA))
    assert timed_out.last_evidence is not None and timed_out.last_evidence["timeout_seconds"] == 5
    assert timed_out.last_evidence["error_code"] == "BACKEND_TIMEOUT"
    failed = MeasuredChatProvider(bound, backend=ScriptedBackend(error="OUTPUT_TRUNCATED"))
    with pytest.raises(MeasuredExecutionFailed, match="OUTPUT_TRUNCATED"):
        asyncio.run(failed.chat("x", response_schema=SCHEMA))


# --- factory -------------------------------------------------------------------------------------------------

def test_factory_refuses_without_a_binding_and_keeps_mock_explicit(world: World) -> None:
    with pytest.raises(ExecutionBindingRefused, match="MEASURED_EXECUTION_INTENT_REQUIRED"):
        build_chat_provider(world.settings())  # a real model without an intent and project is never built
    with pytest.raises(ExecutionBindingRefused, match="MEASURED_PROFILE_STORE_NOT_CONFIGURED"):
        build_chat_provider(world.settings(PADOS_MODEL_CATALOG=""), intent=ExecutionIntent.STRUCTURED_OUTPUT,
                            project_id=PROJECT)
    mock = world.settings(PADOS_CHAT_PROVIDER="mock")
    with pytest.raises(ProviderNotConfiguredError):
        build_chat_provider(mock)
    assert isinstance(build_chat_provider(mock, allow_mock=True), MockProvider)
    for name in ("claude", "codex"):
        with pytest.raises(ProviderNotConfiguredError, match=f"PROVIDER_NOT_CONFIGURED:{name}"):
            build_chat_provider(world.settings(PADOS_CHAT_PROVIDER=name))
    assert world.requests("/api/chat") == [] and world.requests("/api/generate") == []
