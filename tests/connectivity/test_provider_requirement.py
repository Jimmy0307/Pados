"""Connectivity Closure B: provider connectivity requirement (ENFORCED before dispatch)."""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from pados.config import Settings
from pados.connectivity.maker import RequirementGatedMaker, WanObservation
from pados.connectivity.provider_gate import gate_product_loop_maker, product_loop_requirement
from pados.connectivity.requirements import (
    PROVIDER_CONNECTIVITY_METADATA_INVALID,
    NetworkRequirement,
    ProviderMetadataInvalid,
    requirement_for_endpoint,
    resolve_requirement,
)
from pados.connectivity.config import ConnectivityConfig
from pados.connectivity.runtime import ConfiguredWan, start_runtime, stop_runtime
from pados.connectivity.state import ConnectivityState
from pados.enterprise.compute import profile_for_worker
from pados.product_loop.codex_provider import CodexCliMaker
from pados.product_loop.schemas import (
    FailureClass,
    GoalRequest,
    ProductLoopStatus,
    RuntimeResult,
    RuntimeStatus,
)
from pados.product_loop.service import ProductLoopService
from pados.product_loop.state import InMemoryP6_19BStateGateway
from pados.product_loop.validation import FileValidator
from pados.worker.schemas import CapabilityId, RiskLevel, WorkerAvailability, WorkerSpec, WorkerType
from tests.connectivity.connectivity_support import FakeProbes, write_config


@pytest.fixture(autouse=True)
def _no_leaked_runtime() -> Iterator[None]:
    yield
    stop_runtime()


def _worker(worker_id: str, worker_type: WorkerType) -> WorkerSpec:
    return WorkerSpec(
        worker_id=worker_id, worker_type=worker_type, display_name=worker_id,
        availability=WorkerAvailability.AVAILABLE,
        capabilities=[CapabilityId.MODIFY_CODE, CapabilityId.SUMMARIZE], risk_ceiling=RiskLevel.MEDIUM,
        supports_structured_output=True, supports_file_changes=True, supports_tool_calls=False,
        supports_streaming=False, max_context_tokens=8000, default_timeout_seconds=60,
    )


CODEX = _worker("codex-cli", WorkerType.CODEX_CLI)
OLLAMA = _worker("ollama-local", WorkerType.OLLAMA)


class SpyMaker:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.approved_root = Path("/approved")

    async def make(self, contract: Any, *, run_id: str, attempt: int) -> RuntimeResult:
        self.calls.append(run_id)
        return RuntimeResult(run_id=run_id, attempt=attempt, runtime_id="spy", status=RuntimeStatus.COMPLETED,
                             started_at="t", completed_at="t", summary="spy")


class FakeWan:
    """Fresh observation unless ``age`` says otherwise; ``refresh`` applies ``after_observe``."""

    max_age_seconds = 300.0

    def __init__(self, state: ConnectivityState, after_observe: ConnectivityState | None = None, *,
                 age: float | None = 0.0, observable: bool = True) -> None:
        self.state, self.after_observe, self.observations = state, after_observe, 0
        self.age, self.observable = age, observable

    def wan_observation(self) -> WanObservation:
        return WanObservation(observable=self.observable, state=self.state, age_seconds=self.age)

    def refresh(self) -> None:
        self.observations += 1
        if self.after_observe is not None:
            self.state = self.after_observe
        self.age = 0.0


def _make(maker: Any) -> RuntimeResult:
    result: RuntimeResult = asyncio.run(maker.make(None, run_id="run-1", attempt=1))
    return result


def _gated(requirement: NetworkRequirement, wan: FakeWan) -> tuple[RequirementGatedMaker, SpyMaker]:
    inner = SpyMaker()
    return RequirementGatedMaker(inner, requirement=lambda: requirement, wan=wan), inner  # type: ignore[arg-type]


# --- requirement derivation ---------------------------------------------------------------------

def test_requirement_derives_from_profile_locality_not_provider_name() -> None:
    assert resolve_requirement(profile=profile_for_worker(CODEX)) is NetworkRequirement.WAN_REQUIRED
    assert resolve_requirement(profile=profile_for_worker(OLLAMA)) is NetworkRequirement.LOCAL_ONLY
    renamed = _worker("totally-local-sounding", WorkerType.CODEX_CLI)  # the name carries no authority
    assert resolve_requirement(profile=profile_for_worker(renamed)) is NetworkRequirement.WAN_REQUIRED


@pytest.mark.parametrize(("url", "requirement"), [
    ("http://127.0.0.1:11434", NetworkRequirement.LOCAL_ONLY),
    ("http://localhost:8000", NetworkRequirement.LOCAL_ONLY),
    ("http://192.168.1.20:8767", NetworkRequirement.PRIVATE_NETWORK),
    ("http://100.64.0.5:8767", NetworkRequirement.PRIVATE_NETWORK),
    ("https://api.provider.example/v1", NetworkRequirement.WAN_REQUIRED),
    ("https://203.0.113.10", NetworkRequirement.WAN_REQUIRED),
])
def test_endpoint_locality_requirement(url: str, requirement: NetworkRequirement) -> None:
    assert requirement_for_endpoint(url) is requirement
    assert resolve_requirement(endpoint=url) is requirement


def test_missing_metadata_is_conservative_wan_required() -> None:
    assert resolve_requirement() is NetworkRequirement.WAN_REQUIRED
    remote = profile_for_worker(CODEX)
    assert resolve_requirement(profile=remote) is NetworkRequirement.WAN_REQUIRED
    # a REMOTE profile behind a loopback-looking endpoint is still WAN_REQUIRED
    assert resolve_requirement(profile=remote, endpoint="http://127.0.0.1:1") is NetworkRequirement.WAN_REQUIRED


@pytest.mark.parametrize("value", ["SOMETIMES", "", "wan_required", 3, ["WAN_REQUIRED"]])
def test_invalid_operator_metadata_fails_closed(value: object) -> None:
    with pytest.raises(ProviderMetadataInvalid) as info:
        resolve_requirement(profile=profile_for_worker(OLLAMA), explicit=value)
    assert info.value.code == PROVIDER_CONNECTIVITY_METADATA_INVALID


@pytest.mark.parametrize("value", ["LOCAL_ONLY", "NETWORK_OPTIONAL"])
def test_remote_profile_cannot_be_declared_local(value: str) -> None:
    with pytest.raises(ProviderMetadataInvalid):
        resolve_requirement(profile=profile_for_worker(CODEX), explicit=value)


def test_explicit_operator_metadata_is_honoured() -> None:
    assert resolve_requirement(profile=profile_for_worker(CODEX), explicit="PRIVATE_NETWORK") \
        is NetworkRequirement.PRIVATE_NETWORK
    assert resolve_requirement(profile=profile_for_worker(OLLAMA), explicit="NETWORK_OPTIONAL") \
        is NetworkRequirement.NETWORK_OPTIONAL


# --- gate behaviour -----------------------------------------------------------------------------

@pytest.mark.parametrize("state", [ConnectivityState.OFFLINE, ConnectivityState.INTERMITTENT])
def test_wan_required_provider_offline_never_dispatches(state: ConnectivityState) -> None:
    maker, inner = _gated(NetworkRequirement.WAN_REQUIRED, FakeWan(state))
    result = _make(maker)
    assert result.status is RuntimeStatus.FAILED and result.summary == "PROVIDER_UNREACHABLE"
    assert result.failure_classification is FailureClass.RUNTIME_FAILURE and result.retryable is True
    assert inner.calls == []


@pytest.mark.parametrize("requirement", [NetworkRequirement.LOCAL_ONLY, NetworkRequirement.PRIVATE_NETWORK,
                                         NetworkRequirement.NETWORK_OPTIONAL])
def test_local_and_private_providers_run_without_wan(requirement: NetworkRequirement) -> None:
    wan = FakeWan(ConnectivityState.OFFLINE)
    maker, inner = _gated(requirement, wan)
    assert _make(maker).summary == "spy" and inner.calls == ["run-1"] and wan.observations == 0


@pytest.mark.parametrize("state", [ConnectivityState.CONNECTED, ConnectivityState.RECOVERING])
def test_wan_required_provider_dispatches_when_wan_usable(state: ConnectivityState) -> None:
    maker, inner = _gated(NetworkRequirement.WAN_REQUIRED, FakeWan(state))
    assert _make(maker).summary == "spy" and inner.calls == ["run-1"]


def test_unknown_wan_triggers_exactly_one_observation() -> None:
    offline = FakeWan(ConnectivityState.UNKNOWN, after_observe=ConnectivityState.OFFLINE, age=None)
    maker, inner = _gated(NetworkRequirement.WAN_REQUIRED, offline)
    assert _make(maker).summary == "PROVIDER_UNREACHABLE" and offline.observations == 1 and inner.calls == []
    online = FakeWan(ConnectivityState.UNKNOWN, after_observe=ConnectivityState.CONNECTED, age=None)
    maker, inner = _gated(NetworkRequirement.WAN_REQUIRED, online)
    assert _make(maker).summary == "spy" and online.observations == 1
    still_unknown = FakeWan(ConnectivityState.UNKNOWN, after_observe=ConnectivityState.UNKNOWN, age=None)
    maker, inner = _gated(NetworkRequirement.WAN_REQUIRED, still_unknown)
    assert _make(maker).summary == "PROVIDER_UNREACHABLE" and still_unknown.observations == 1  # fail closed


def test_unknown_wan_observation_via_configured_monitor(tmp_path: Path) -> None:
    probes = FakeProbes(wan=False)
    config = ConnectivityConfig.load(write_config(tmp_path))
    wan = ConfiguredWan(write_config(tmp_path), config, probe_factory=probes.factory)
    maker, inner = _gated(NetworkRequirement.WAN_REQUIRED, wan)  # type: ignore[arg-type]
    assert _make(maker).summary == "PROVIDER_UNREACHABLE" and probes.calls == ["WAN", "OVERLAY"]
    assert _make(maker).summary == "PROVIDER_UNREACHABLE" and probes.calls == ["WAN", "OVERLAY"]  # still fresh
    assert inner.calls == []


def test_metadata_invalid_blocks_without_execution() -> None:
    inner = SpyMaker()

    def invalid() -> NetworkRequirement:
        raise ProviderMetadataInvalid()

    maker = RequirementGatedMaker(inner, requirement=invalid, wan=FakeWan(ConnectivityState.CONNECTED))  # type: ignore[arg-type]
    result = _make(maker)
    assert result.status is RuntimeStatus.BLOCKED and result.summary == PROVIDER_CONNECTIVITY_METADATA_INVALID
    assert result.failure_classification is FailureClass.AUTHORITY_BLOCK and result.retryable is False
    assert inner.calls == []


# --- default product-loop wiring ----------------------------------------------------------------

def _settings(config_path: str, tmp_path: Path) -> Settings:
    return Settings(_env_file=None, PADOS_CONNECTIVITY_CONFIG=config_path,
                    PADOS_PRODUCT_WORKSPACE_ROOT=str(tmp_path / "ws"),
                    PADOS_PRODUCT_OBJECT_ROOT=str(tmp_path / "objects"),
                    PADOS_PRODUCT_DATABASE_NAME="closure_test", PADOS_DB_NAME="closure_ops")


def _codex(tmp_path: Path, settings: Settings, runner_calls: list[Any],
           workers: dict[str, WorkerSpec] | None = None) -> CodexCliMaker:
    def runner(*args: Any, **kwargs: Any) -> Any:
        runner_calls.append(args)
        raise AssertionError("the provider must not be invoked")

    (tmp_path / "ws").mkdir(exist_ok=True)
    return CodexCliMaker(tmp_path / "ws", settings=settings, runner=runner,
                         worker_discovery=lambda _s: dict(workers or {"codex-cli": CODEX}),
                         executable_resolver=lambda: "/nonexistent/codex")


def test_product_loop_requirement_comes_from_router_selected_worker_profile(tmp_path: Path) -> None:
    config = ConnectivityConfig.load(write_config(tmp_path))
    settings = Settings(_env_file=None)
    both = {"codex-cli": CODEX, "ollama-local": OLLAMA}
    assert product_loop_requirement(settings, lambda _s: both, config) is NetworkRequirement.WAN_REQUIRED
    assert product_loop_requirement(settings, lambda _s: {}, config) is NetworkRequirement.WAN_REQUIRED
    explicit = ConnectivityConfig.load(write_config(tmp_path, provider_requirements={"codex-cli": "BOGUS"}))
    with pytest.raises(ProviderMetadataInvalid):
        product_loop_requirement(settings, lambda _s: both, explicit)


def test_unconfigured_product_loop_maker_is_unchanged(tmp_path: Path) -> None:
    settings = _settings("", tmp_path)
    maker = _codex(tmp_path, settings, [])
    assert gate_product_loop_maker(maker, settings) is maker
    from pados.product_loop.routes import build_product_loop_service

    assert isinstance(build_product_loop_service(settings).maker, CodexCliMaker)


def test_build_product_loop_service_wraps_codex_when_configured(tmp_path: Path) -> None:
    from pados.product_loop.routes import build_product_loop_service

    service = build_product_loop_service(_settings(write_config(tmp_path), tmp_path))
    assert isinstance(service.maker, RequirementGatedMaker) and isinstance(service.maker.inner, CodexCliMaker)
    assert service.maker.approved_root == (tmp_path / "ws").resolve()


def _goal(workspace: Path) -> GoalRequest:
    return GoalRequest.model_validate({
        "goal": "cloud job", "workspace_root": str(workspace), "scope_id": "proj-closure",
        "changes": [{"path": "app.py", "content": "def result():\n    return 'good'\n"}],
        "acceptance_criteria": [{"criterion_id": "c", "check": "FILE_CONTAINS", "target_path": "app.py",
                                 "expected_text": "return 'good'"}], "authorized": True})


def test_default_wiring_wan_offline_fails_closed_without_fallback(tmp_path: Path) -> None:
    path = write_config(tmp_path, background={"enabled": True})
    probes = FakeProbes(wan=False)
    runtime = start_runtime(path, start_thread=False, probe_factory=probes.factory)
    assert runtime is not None
    runtime.probe.run_once()
    settings = _settings(path, tmp_path)
    runner_calls: list[Any] = []
    local = SpyMaker()  # an available local alternative that must never be touched
    maker = gate_product_loop_maker(
        _codex(tmp_path, settings, runner_calls, {"codex-cli": CODEX, "ollama-local": OLLAMA}), settings)
    gateway = InMemoryP6_19BStateGateway(tmp_path / "objects")
    loop = ProductLoopService(maker=maker, validator=FileValidator(), state_gateway=gateway)
    report = asyncio.run(loop.execute(_goal(tmp_path / "ws")))
    assert report.status is not ProductLoopStatus.COMMITTED
    assert report.final_result is not None and report.final_result.summary == "PROVIDER_UNREACHABLE"
    assert report.final_result.runtime_id.startswith("connectivity-gate-")
    assert runner_calls == [] and local.calls == [] and not gateway.commits
    assert not (tmp_path / "ws" / "app.py").exists()


def test_default_wiring_invalid_metadata_and_invalid_config_block(tmp_path: Path) -> None:
    runner_calls: list[Any] = []
    bad_meta = write_config(tmp_path, provider_requirements={"codex-cli": "SOMETIMES"})
    settings = _settings(bad_meta, tmp_path)
    result = _make(gate_product_loop_maker(_codex(tmp_path, settings, runner_calls), settings))
    assert result.status is RuntimeStatus.BLOCKED and result.summary == PROVIDER_CONNECTIVITY_METADATA_INVALID
    bad_config = write_config(tmp_path, wan_probe={"host": "169.254.169.254", "port": 80})
    settings = _settings(bad_config, tmp_path)
    result = _make(gate_product_loop_maker(_codex(tmp_path, settings, runner_calls), settings))
    assert result.status is RuntimeStatus.BLOCKED and result.summary == "CONNECTIVITY_CONFIG_INVALID"
    assert runner_calls == []


def test_cr03_cc02_configured_without_wan_probe_refuses_wan_required(tmp_path: Path) -> None:
    inner = SpyMaker()
    path = write_config(tmp_path, wan_probe=None)
    config = ConnectivityConfig.load(path)
    probes = FakeProbes(wan=False)
    maker = RequirementGatedMaker(inner, requirement=lambda: NetworkRequirement.WAN_REQUIRED,  # type: ignore[arg-type]
                                  wan=ConfiguredWan(path, config, probe_factory=probes.factory))
    result = _make(maker)
    assert result.summary == "PROVIDER_UNREACHABLE" and result.retryable is True and inner.calls == []
    assert maker.diagnostics[-1]["reason"] == "WAN_NOT_OBSERVABLE" and probes.calls == []
    local = RequirementGatedMaker(inner, requirement=lambda: NetworkRequirement.LOCAL_ONLY,  # type: ignore[arg-type]
                                  wan=ConfiguredWan(path, config, probe_factory=probes.factory))
    assert _make(local).summary == "spy"  # non-WAN providers are unaffected
