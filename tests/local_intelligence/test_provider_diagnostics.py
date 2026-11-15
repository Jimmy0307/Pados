# SPDX-License-Identifier: AGPL-3.0-only
"""Provider diagnostics: reachable vs measured vs authorized per discovered model, the real consumer resolver for
the configured chat model, and the owner's setup narrowing (disabled provider, project scope).

Real components: describe_runtime over the loopback FakeOllama, ModelCatalogStore, bind_live, the compute-node registry
and resolve_bound_execution. The device is a fake host: never model evidence."""
from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_ollama import FakeOllama  # noqa: E402
from live_chain_support import Clock, fake_profiler, persist, rehearsal_profile  # noqa: E402

from pados.compute_node.manifest import NodeCapability  # noqa: E402
from pados.compute_node.registry import NodeRegistration  # noqa: E402
from pados.config import Settings  # noqa: E402
from pados.enterprise.classification import DataClassification  # noqa: E402
from pados.providers.diagnostics import ProviderDiagnostics  # noqa: E402
from pados.providers.execution_binding import (  # noqa: E402
    ExecutionBindingRefused,
    ExecutionIntent,
    resolve_bound_execution,
)
from pados.providers.setup import ProviderSettings, ProvidersSetupStore, SetupError, parse_settings, upsert  # noqa: E402
from pados.resources.capability_profile import ModelCapabilityProfile  # noqa: E402

PROJECT = "11111111-2222-4333-8444-555555555555"


class Env:
    def __init__(self, tmp: Path, fake: FakeOllama, profile: ModelCapabilityProfile, clock: Clock) -> None:
        self.tmp, self.fake, self.profile, self.clock = tmp, fake, profile, clock

    def settings(self, **overrides: object) -> Settings:
        values: dict[str, object] = {
            "PADOS_ENV": "test", "PADOS_CHAT_PROVIDER": "ollama", "PADOS_CHAT_MODEL": self.profile.artifact.artifact_ref,
            "OLLAMA_BASE_URL": self.fake.url, "PADOS_MODEL_CATALOG": str(self.tmp / "catalog.json"),
            "PADOS_COMPUTE_NODES_CONFIG": str(self.tmp / "nodes.json"),
            "PADOS_PROVIDERS_SETUP": str(self.tmp / "local" / "providers.json")}
        values.update(overrides)
        return Settings.model_validate(values)

    def register(self, *, authorized: bool = True) -> None:
        registration = NodeRegistration(
            node_id="lcn-" + "1" * 20, endpoint=self.fake.url, auth_ref="secretref://env/PADOS_TEST_NODE_KEY",
            authorized=authorized, allowed_capabilities=(NodeCapability.LOCAL_INFERENCE,),
            max_classification=DataClassification.INTERNAL, project_ids=(PROJECT,),
            allowed_models=(self.profile.artifact.model_ref,), security_profile="LOCAL_SINGLE_USER")  # type: ignore[arg-type]
        (self.tmp / "nodes.json").write_text(json.dumps({
            "schema_version": "pados.compute-nodes.registry.v1", "state_dir": str(self.tmp / "state"),
            "nodes": [registration.model_dump(mode="json")]}), encoding="utf-8")

    def diagnostics(self, **overrides: object) -> ProviderDiagnostics:
        return ProviderDiagnostics(self.settings(**overrides), profiler=fake_profiler(self.clock), clock=self.clock)

    def setup(self, **fields: Any) -> None:
        store = ProvidersSetupStore(self.tmp / "local" / "providers.json")
        item = parse_settings({"provider_id": "ollama-local", "kind": "ollama", "endpoint": self.fake.url, **fields})
        store.update(store.load().revision, lambda setup: upsert(setup, item))


def make_env(tmp: Path, **fake: Any) -> Iterator[Env]:
    profile = rehearsal_profile()
    persist(tmp / "catalog.json", profile)
    with FakeOllama(present=True, **fake) as runtime:
        env = Env(tmp, runtime, profile, Clock())
        env.register()
        yield env


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    yield from make_env(tmp_path)


def _model(view: dict[str, Any]) -> dict[str, Any]:
    (model,) = view["models"]
    return model


def _check(view: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in view["checks"] if item["check"] == name)


def test_detected_provider_reachable_measured_and_authorized(env: Env) -> None:
    env.setup(enabled=True, project_ids=[PROJECT], capability_requirement="STRUCTURED_OUTPUT")
    view = env.diagnostics().diagnose("ollama-local")
    assert view["reachability"]["state"] == "REACHABLE" and view["reachability"]["runtime_version"] == "0.34.3"
    model = _model(view)
    assert model["governance"] == "AUTHORIZED_AND_MEASURED" and model["profile_id"] == env.profile.profile_id
    assert model["authorized_by"] == ["lcn-" + "1" * 20] and model["authority"] == "MEASURED_PROFILE"
    binding = _check(view, "governed_binding")
    assert binding["status"] == "BOUND" and binding["profile_id"] == env.profile.profile_id
    assert _check(view, "model_identity")["status"] == "PASS"


def test_reachable_but_not_authorized_is_shown_separately(env: Env) -> None:
    env.register(authorized=False)
    view = env.diagnostics().diagnose("ollama-local")
    assert view["reachability"]["state"] == "REACHABLE"
    assert _model(view)["governance"] == "MEASURED_NOT_AUTHORIZED"
    assert _check(view, "governed_binding")["status"] == "REFUSED"


def test_other_runtime_version_is_measured_but_not_applicable(tmp_path: Path) -> None:
    for env in make_env(tmp_path, version="0.40.16"):
        model = _model(env.diagnostics().diagnose("ollama-local"))
        assert model["governance"] == "MEASURED_NOT_APPLICABLE"
        assert "RUNTIME_VERSION_NOT_MEASURED" in model["reasons"]


def test_unmeasured_digest_is_a_non_authoritative_observation(tmp_path: Path) -> None:
    for env in make_env(tmp_path, digest_hex="ab" * 32):
        model = _model(env.diagnostics().diagnose("ollama-local"))
        assert model["governance"] == "NOT_MEASURED" and model["authority"] == "NON_AUTHORITATIVE_OBSERVATION"
        assert model["reasons"] == ["MEASURED_ARTIFACT_DIGEST_DIFFERS"]


def test_unreachable_runtime_and_disabled_provider(env: Env, tmp_path: Path) -> None:
    diagnostics = env.diagnostics(OLLAMA_BASE_URL="http://127.0.0.1:9")
    view = diagnostics.diagnose("ollama-local")
    assert view["reachability"]["state"] == "UNREACHABLE" and view["reachability"]["explanation"]
    env.setup(enabled=False)
    assert env.diagnostics().diagnose("ollama-local")["reachability"]["state"] == "DISABLED"


def _resolve(env: Env, project: str = PROJECT) -> Any:
    return resolve_bound_execution(env.settings(), ExecutionIntent.STRUCTURED_OUTPUT, project_id=project,
                                   live_factory=lambda s: _live(env, s), now=lambda: env.clock.now)


def _live(env: Env, settings: Settings) -> Any:
    from pados.local_intelligence.live import LiveLocalState
    from pados.local_intelligence.runtimes import default_runtime_endpoints

    return LiveLocalState(profiler=fake_profiler(env.clock), endpoints=default_runtime_endpoints(settings.ollama_base_url),
                          clock=env.clock)


def test_owner_setup_can_only_narrow_the_consumer_binding(env: Env) -> None:
    assert _resolve(env).profile_id == env.profile.profile_id  # no setup entry: unchanged
    env.setup(enabled=False, project_ids=[PROJECT])
    with pytest.raises(ExecutionBindingRefused, match="PROVIDER_DISABLED"):
        _resolve(env)
    env.setup(enabled=True, project_ids=["22222222-2222-4222-8222-222222222222"])
    with pytest.raises(ExecutionBindingRefused, match="PROVIDER_PROJECT_NOT_IN_SCOPE"):
        _resolve(env)
    env.setup(enabled=True, project_ids=[PROJECT])
    assert _resolve(env).profile_id == env.profile.profile_id
    (env.tmp / "local" / "providers.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ExecutionBindingRefused, match="PROVIDERS_SETUP_INVALID"):
        _resolve(env)


def test_setup_store_refuses_secrets_and_stale_revisions(tmp_path: Path) -> None:
    store = ProvidersSetupStore(tmp_path / "providers.json")
    with pytest.raises(SetupError, match="PROVIDER_SETTINGS_CONTAIN_KEY_VALUE"):
        parse_settings({"provider_id": "x", "kind": "ollama", "endpoint": "http://127.0.0.1:1",
                        "display_name": "sk-" + "a" * 20})
    with pytest.raises(SetupError, match="PROVIDER_ENDPOINT_MUST_BE_PRIVATE"):
        parse_settings({"provider_id": "x", "kind": "ollama", "endpoint": "https://public.example.invalid"})
    with pytest.raises(SetupError, match="PROVIDER_GATEWAY_CONTRACT_PATH_REQUIRED"):
        parse_settings({"provider_id": "g", "kind": "enterprise_gateway"})
    item = ProviderSettings(provider_id="x", kind="ollama", endpoint="http://127.0.0.1:1", auth_env="PADOS_X_KEY")
    assert item.auth_ref == "secretref://env/PADOS_X_KEY"
    store.update(0, lambda setup: upsert(setup, item))
    with pytest.raises(SetupError, match="PROVIDERS_SETUP_REVISION_CONFLICT"):
        store.update(0, lambda setup: upsert(setup, item))
    assert store.load().revision == 1
