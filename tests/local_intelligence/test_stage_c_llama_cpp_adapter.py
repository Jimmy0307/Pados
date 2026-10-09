"""Stage C runtime #2: the native llama.cpp-server adapter, its runtime probe and the PADOS-derived artifact digest.

Against a loopback fake server only (``FakeLlamaServer``); no model, no real runtime.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "stage_b"))
from capability_profile_support import measured_config  # noqa: E402
from fake_llama_server import FakeLlamaServer  # noqa: E402
from li_fakes import NOW  # noqa: E402
from m4_1_live_chain import DOGFOOD_MESSAGES, DOGFOOD_SCHEMA  # noqa: E402
from stage_c_support import LLAMA_FAMILY, LLAMA_VERSION, gguf_file, llama_profile  # noqa: E402

from pados.compute_node.artifact_identity import (  # noqa: E402
    ARTIFACT_MANIFEST_SCHEMA,
    LocalArtifactIdentity,
    manifest_digest_for,
)
from pados.compute_node.backends import (  # noqa: E402
    BACKEND_KINDS,
    BackendError,
    LlamaCppServerBackend,
    OpenAICompatibleBackend,
    llama_cpp_chat_body,
    llama_cpp_version,
)
from pados.compute_node.config import BackendConfig  # noqa: E402
from pados.compute_node.manifest import NodeCapability  # noqa: E402
from pados.compute_node.public import PublicBackend, public_backend  # noqa: E402
from pados.local_intelligence.live import runtime_observation  # noqa: E402
from pados.local_intelligence.runtimes import (  # noqa: E402
    RUNTIME_PROBES,
    RuntimeEndpoint,
    RuntimeHealth,
    describe_runtime,
)
from pados.resources.capability_profile import (  # noqa: E402
    BindingPolicy,
    CandidateState,
    DeviceObservation,
    MeasuredCapability,
    OutputConstraint,
    ReasoningMode,
    bind_live,
)

EXTRACTION = MeasuredCapability.INFORMATION_EXTRACTION
ITEM = {"messages": DOGFOOD_MESSAGES, "response_schema": DOGFOOD_SCHEMA}
ALIAS = "brand-b:4b"


@pytest.fixture
def model(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "models"
    return root, gguf_file(root, "brand-b-4b.gguf", "b")


@pytest.fixture
def server(model: tuple[Path, Path]) -> Iterator[FakeLlamaServer]:
    with FakeLlamaServer(alias=ALIAS, model_path=str(model[1])) as fake:
        yield fake


def backend(fake: FakeLlamaServer, root: Path | None = None,
            pins: dict[str, str | None] | None = None) -> LlamaCppServerBackend:
    return LlamaCppServerBackend("llama", fake.url, artifact_root=str(root) if root else None, pinned_digests=pins)


def pinned(fake: FakeLlamaServer) -> tuple[Path, dict[str, str | None]]:
    """The artifact root and the pin a node would hold for the file the fake serves NOW (discovery time)."""
    root = Path(fake.model_path).parent
    artifact = LocalArtifactIdentity(root).identify(fake.model_path)
    assert artifact is not None
    return root, {ALIAS: artifact.manifest_digest}


def run(fake: FakeLlamaServer, *, budget: int = 256, execution: Any = "bound", item: dict[str, Any] | None = None,
        model: str = ALIAS, root: Path | None = None, pins: dict[str, str | None] | None = None) -> Any:
    config = measured_config(EXTRACTION, max_output_tokens=budget) if execution == "bound" else execution
    if root is None and pins is None:
        root, pins = pinned(fake)
    return backend(fake, root, pins).run(NodeCapability.LOCAL_INFERENCE, model, item or ITEM,
                                         max_output_tokens=budget, timeout_seconds=10, execution=config)


# --- translation -------------------------------------------------------------------------------------------------

def test_generic_configuration_translates_into_the_server_request_only_here() -> None:
    config = measured_config(EXTRACTION, max_output_tokens=300)
    body = llama_cpp_chat_body("m", [{"role": "user", "content": "x"}], max_output_tokens=300, execution=config,
                               schema={"type": "object"})
    assert body == {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": False, "max_tokens": 300,
                    "temperature": 0.0, "chat_template_kwargs": {"enable_thinking": False}, "seed": 42,
                    "response_format": {"type": "json_schema",
                                        "json_schema": {"name": "pados_output", "schema": {"type": "object"}}}}
    enabled = config.model_copy(update={"reasoning_mode": ReasoningMode.ENABLED, "seed": None,
                                        "output_constraint": OutputConstraint.UNCONSTRAINED})
    body = llama_cpp_chat_body("m", [], max_output_tokens=300, execution=enabled, schema=None)
    assert body["chat_template_kwargs"] == {"enable_thinking": True} and "seed" not in body
    assert "response_format" not in body
    assert llama_cpp_chat_body("m", [], max_output_tokens=64, execution=None, schema=None) == {
        "model": "m", "messages": [], "max_tokens": 64, "temperature": 0, "stream": False}


def test_bound_run_verifies_the_window_and_echoes_what_it_requested(server: FakeLlamaServer) -> None:
    out = run(server)
    assert json.loads(out.content)["invoice_number"] == "INV-88213"
    config = measured_config(EXTRACTION, max_output_tokens=256)
    assert out.runtime["execution_requested"] == config.model_dump(mode="json")
    assert out.runtime["context_window_verified"] == 8192 and out.runtime["backend"] == LLAMA_FAMILY
    assert out.runtime["done_reason"] == "stop" and len(out.runtime["response_schema_sha256"]) == 64


def test_unbound_request_keeps_the_generic_shape_and_reproduces_the_empty_output_failure(
        model: tuple[Path, Path]) -> None:
    with FakeLlamaServer(alias=ALIAS, model_path=str(model[1]), reasoning_tokens=400) as fake:
        with pytest.raises(BackendError) as failed:
            run(fake, execution=None, item={"messages": DOGFOOD_MESSAGES})
        assert failed.value.code == "EMPTY_COMPLETION"
        assert failed.value.detail["done_reason"] == "length" and failed.value.detail["thinking_chars"] == 256
        (body,) = fake.chats()
        assert "chat_template_kwargs" not in body  # reasoning was not disabled: it spent the whole budget
        assert json.loads(run(fake).content)["total"] == 48350  # the bound request disables it and succeeds


# --- observable checks --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("inline", [False, True], ids=["reasoning-field", "inline-think-block"])
def test_reasoning_despite_the_switch_is_refused(model: tuple[Path, Path], inline: bool) -> None:
    with FakeLlamaServer(alias=ALIAS, model_path=str(model[1]), reasoning_tokens=50, thinking_despite_disabled=True,
                         think_inline=inline) as fake:
        with pytest.raises(BackendError, match="^EXECUTION_CONFIG_NOT_APPLIED:reasoning_mode$") as refused:
            run(fake)
        assert refused.value.detail["thinking_chars"] == 50


def test_truncated_and_empty_answers_are_refused(model: tuple[Path, Path]) -> None:
    with FakeLlamaServer(alias=ALIAS, model_path=str(model[1]), answer_tokens=300) as fake:
        with pytest.raises(BackendError, match="^OUTPUT_TRUNCATED$") as truncated:
            run(fake)
        assert truncated.value.detail["done_reason"] == "length"
    with FakeLlamaServer(alias=ALIAS, model_path=str(model[1]), empty_content=True) as fake:
        with pytest.raises(BackendError, match="^EMPTY_COMPLETION$"):
            run(fake)


# --- the context window is verified, never assumed ----------------------------------------------------------------

def test_context_window_mismatch_unknown_or_changed_fails_closed(model: tuple[Path, Path]) -> None:
    with FakeLlamaServer(alias=ALIAS, model_path=str(model[1]), n_ctx=16384) as fake:
        with pytest.raises(BackendError, match="^EXECUTION_CONFIG_MISMATCH:context_window$") as refused:
            run(fake)
        assert refused.value.detail == {"runtime_context_window": 16384, "measured_context_window": 8192}
        assert fake.chats() == []
    with FakeLlamaServer(alias=ALIAS, model_path=str(model[1]), props_available=False) as fake:
        with pytest.raises(BackendError, match="^EXECUTION_CONFIG_UNVERIFIABLE:context_window$"):
            run(fake)
        assert fake.chats() == []
    with FakeLlamaServer(alias=ALIAS, model_path=str(model[1]), extra_models=("second-model",)) as fake:
        with pytest.raises(BackendError, match="^EXECUTION_CONFIG_UNVERIFIABLE:context_window$"):
            run(fake)  # several served models: the window cannot be attributed to the requested one
    with FakeLlamaServer(alias=ALIAS, model_path=str(model[1]), restart_after_chat=4096) as fake:
        with pytest.raises(BackendError, match="^RUNTIME_CHANGED_DURING_EXECUTION$"):
            run(fake)


def test_the_served_model_must_be_the_requested_one(server: FakeLlamaServer) -> None:
    with pytest.raises(BackendError, match="^MODEL_NOT_FOUND$"):
        run(server, model="another-model:1b")  # the server would answer with whatever it loaded
    assert server.chats() == []


# --- red-team F-1: the served FILE is re-verified against the pinned digest around every bound call ---------------

def test_bound_run_records_the_verified_artifact_digest(server: FakeLlamaServer) -> None:
    _, pins = pinned(server)
    assert run(server).runtime["artifact_digest_verified"] == pins[ALIAS]


def test_another_gguf_under_the_same_alias_is_refused_before_the_call(server: FakeLlamaServer,
                                                                       model: tuple[Path, Path]) -> None:
    root, pins = pinned(server)  # discovery saw the measured file
    server.model_path = str(gguf_file(root, "other-4b.gguf", "other"))  # restarted with another file, same alias
    with pytest.raises(BackendError, match="^MODEL_ARTIFACT_MISMATCH$"):
        run(server, root=root, pins=pins)
    assert server.chats() == []


def test_a_served_file_outside_the_artifact_root_is_refused(tmp_path: Path, server: FakeLlamaServer) -> None:
    root, pins = pinned(server)
    server.model_path = str(gguf_file(tmp_path / "elsewhere", "brand-b-4b.gguf", "b"))  # same bytes, outside root
    with pytest.raises(BackendError, match="^MODEL_ARTIFACT_UNVERIFIABLE$"):
        run(server, root=root, pins=pins)
    assert server.chats() == []


def test_a_bound_run_without_an_identity_source_or_pin_is_refused(server: FakeLlamaServer) -> None:
    root, pins = pinned(server)
    for no_root, no_pins in ((None, pins), (root, {}), (root, {ALIAS: None})):
        with pytest.raises(BackendError, match="^MODEL_ARTIFACT_NOT_PRESENT$"):
            backend(server, no_root, no_pins).run(
                NodeCapability.LOCAL_INFERENCE, ALIAS, ITEM, max_output_tokens=256, timeout_seconds=10,
                execution=measured_config(EXTRACTION, max_output_tokens=256))
    assert server.chats() == []


def test_a_file_swapped_during_the_call_yields_no_output(model: tuple[Path, Path]) -> None:
    root, path = model
    other = gguf_file(root, "other-4b.gguf", "other")
    with FakeLlamaServer(alias=ALIAS, model_path=str(path), model_path_after_chat=str(other)) as fake:
        _, pins = pinned(fake)
        with pytest.raises(BackendError, match="^RUNTIME_CHANGED_DURING_EXECUTION$"):
            run(fake, root=root, pins=pins)
        assert len(fake.chats()) == 1


def test_the_node_pins_each_native_model_from_configuration() -> None:
    from pados.compute_node.config import NodeConfig
    from pados.compute_node.service import pinned_digests

    def model(name: str, digest: str | None) -> dict[str, Any]:
        return {"backend_id": "n", "backend_model": name, "family": "f", "version": "1", "context_limit_tokens": 8,
                "tasks": ["LOCAL_INFERENCE"], "resource_class": "CPU_LIGHT", "privacy_class": "INTERNAL",
                "digest": digest}

    a, b = "sha256:" + "a" * 64, "sha256:" + "b" * 64
    config = NodeConfig.model_validate({
        "node_name": "n", "state_dir": "state", "credential_ref": "secretref://env/PADOS_TEST_NODE_CREDENTIAL",
        "capabilities": ["LOCAL_INFERENCE"],
        "backends": [{"backend_id": "n", "kind": LLAMA_FAMILY, "endpoint": "http://127.0.0.1:8080"}],
        "models": [model("one", a), model("two", a), model("two", b), model("three", None)]})
    assert pinned_digests(config, "n") == {"one": a, "two": None, "three": None}  # conflicting pins -> none
    assert pinned_digests(config, "other") == {}


def test_the_generic_openai_compatible_backend_still_refuses_and_existing_kinds_are_unchanged() -> None:
    config = measured_config(EXTRACTION, max_output_tokens=64)
    with pytest.raises(BackendError, match="EXECUTION_CONFIG_UNVERIFIABLE"):
        OpenAICompatibleBackend("o", "http://127.0.0.1:1", transport=lambda *a: (200, {})).run(
            NodeCapability.LOCAL_INFERENCE, "m", ITEM, max_output_tokens=64, timeout_seconds=1, execution=config)
    legacy = BACKEND_KINDS["llama_cpp"]("l", endpoint="http://127.0.0.1:1")
    assert isinstance(legacy, OpenAICompatibleBackend) and legacy.kind == "llama_cpp"
    native = BACKEND_KINDS[LLAMA_FAMILY]("n", endpoint="http://127.0.0.1:1")
    assert isinstance(native, LlamaCppServerBackend) and native.kind == LLAMA_FAMILY
    assert public_backend(LLAMA_FAMILY) is PublicBackend.LLAMA_CPP_BACKEND
    assert set(RUNTIME_PROBES) <= set(BACKEND_KINDS)


def test_build_info_maps_to_a_version_label_or_unknown() -> None:
    assert llama_cpp_version("b6123-1a2b3c4") == LLAMA_VERSION
    assert llama_cpp_version("b42") == "42"
    for bad in (None, 6123, "6123", "b6123-XYZ", "b1-" + "a" * 50, "dev"):
        assert llama_cpp_version(bad) is None


# --- artifact identity: derived from bytes under an operator root; never fabricated -------------------------------

def test_artifact_digest_is_the_canonical_manifest_of_the_file_bytes(model: tuple[Path, Path]) -> None:
    root, path = model
    artifact = LocalArtifactIdentity(root).identify(str(path))
    assert artifact is not None
    data = path.read_bytes()
    layer = "sha256:" + hashlib.sha256(data).hexdigest()
    manifest = json.dumps({"schema": ARTIFACT_MANIFEST_SCHEMA, "media_type": "application/x-gguf",
                           "layers": [{"digest": layer, "size_bytes": len(data)}]}, sort_keys=True,
                          separators=(",", ":"))
    assert artifact.layer_digest == layer and artifact.size_bytes == len(data)
    assert artifact.manifest_digest == "sha256:" + hashlib.sha256(manifest.encode()).hexdigest()
    assert artifact.manifest_digest == manifest_digest_for(layer, len(data))
    copy = gguf_file(root / "elsewhere", "renamed.gguf", "b")  # same bytes, other name: same artifact
    assert LocalArtifactIdentity(root).identify(str(copy)) == artifact


def test_no_digest_is_fabricated(tmp_path: Path, model: tuple[Path, Path]) -> None:
    root, path = model
    identity = LocalArtifactIdentity(root)
    outside = gguf_file(tmp_path / "outside", "x.gguf", "b")
    not_gguf = root / "fake.gguf"
    not_gguf.write_bytes(b"NOPE" + b"0" * 64)
    split = gguf_file(root, "big-00001-of-00002.gguf", "s")
    other_suffix = root / "weights.bin"
    other_suffix.write_bytes(b"GGUF" + b"0" * 64)
    for reported in (str(outside), str(not_gguf), str(split), str(other_suffix), "brand-b-4b.gguf",
                     str(root / ".." / "outside" / "x.gguf"), str(root / "missing.gguf"), None, 7, "",
                     str(path) + "\x00", "x" * 2000):
        assert identity.identify(reported) is None, reported
    if hasattr(os, "symlink"):
        link = root / "link.gguf"
        try:
            link.symlink_to(path)
        except OSError:
            pass  # no symlink privilege on this host
        else:
            assert identity.identify(str(link)) is None
    with pytest.raises(ValueError, match="ARTIFACT_ROOT_MUST_BE_ABSOLUTE"):
        LocalArtifactIdentity("relative/models")


def test_a_changed_file_is_a_different_artifact(model: tuple[Path, Path]) -> None:
    root, path = model
    before = LocalArtifactIdentity(root).identify(str(path))
    path.write_bytes(b"GGUF" + b"changed" * 100)
    after = LocalArtifactIdentity(root).identify(str(path))
    assert before is not None and after is not None and before.manifest_digest != after.manifest_digest


def test_an_in_place_rewrite_that_restores_the_mtime_is_not_served_from_the_cache(
        model: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Red-team F-2: the cache key includes the status-change time. POSIX advances ``st_ctime`` on any write and
    ``utime`` cannot reset it; Windows reports the creation time there, so the advance is simulated portably."""
    from pados.compute_node import artifact_identity

    root, path = model
    before = LocalArtifactIdentity(root).identify(str(path))
    assert before is not None
    stamp = path.stat()
    path.write_bytes(b"GGUF" + b"x" * (stamp.st_size - 4))  # same size, other bytes
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))  # the mtime is restored
    if os.name == "nt":
        real_fstat, real_stat = os.fstat, Path.stat
        monkeypatch.setattr(artifact_identity.os, "fstat", lambda fd: _ctime_advanced(real_fstat(fd)))
        monkeypatch.setattr(Path, "stat", lambda self, **kw: _ctime_advanced(real_stat(self, **kw)))
    after = LocalArtifactIdentity(root).identify(str(path))
    assert after is not None and after.manifest_digest != before.manifest_digest


def test_a_file_whose_open_handle_changes_while_hashed_has_no_digest(
        model: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Red-team F-2: the OPEN file is re-stat'ed after hashing; any change of its signature gives no digest."""
    from pados.compute_node import artifact_identity

    root, path = model
    path.write_bytes(b"GGUF" + b"fresh" * 999)  # not cached yet
    real_fstat = os.fstat
    calls: list[int] = []

    def drifting(fd: int) -> os.stat_result:
        calls.append(fd)
        result = real_fstat(fd)
        return result if len(calls) == 1 else _ctime_advanced(result)

    monkeypatch.setattr(artifact_identity.os, "fstat", drifting)
    assert LocalArtifactIdentity(root).identify(str(path)) is None


def _ctime_advanced(result: os.stat_result) -> os.stat_result:
    """The same stat with a later status-change time (what POSIX reports after any write)."""
    return os.stat_result(tuple(result)[:10], {"st_atime_ns": result.st_atime_ns, "st_mtime_ns": result.st_mtime_ns,
                                               "st_ctime_ns": result.st_ctime_ns + 1})


def test_node_discovery_reports_a_digest_only_with_a_verifiable_source(server: FakeLlamaServer,
                                                                       model: tuple[Path, Path]) -> None:
    root, path = model
    expected = LocalArtifactIdentity(root).identify(str(path))
    assert expected is not None
    assert backend(server).health()
    (found,) = backend(server, root).discover()
    assert (found.backend_model, found.digest) == (ALIAS, expected.manifest_digest)
    (no_root,) = backend(server).discover()
    assert no_root.digest is None
    server.props_available = False
    assert backend(server, root).discover()[0].digest is None


def test_config_scopes_the_artifact_root_to_the_native_kind(tmp_path: Path) -> None:
    good = BackendConfig(backend_id="n", kind=LLAMA_FAMILY, endpoint="http://127.0.0.1:8080",
                         artifact_root=str(tmp_path))
    assert good.artifact_root == str(tmp_path)
    for kind, root in (("ollama", str(tmp_path)), ("llama_cpp", str(tmp_path)), (LLAMA_FAMILY, "relative/models")):
        with pytest.raises(ValueError):
            BackendConfig(backend_id="n", kind=kind, endpoint="http://127.0.0.1:8080", artifact_root=root)
    with pytest.raises(ValueError):
        RuntimeEndpoint(runtime_id="rt-x", runtime_type="ollama", endpoint="http://127.0.0.1:1",
                        artifact_root=str(tmp_path))


# --- runtime discovery + generic binding by digest ----------------------------------------------------------------

def test_runtime_probe_describes_the_server_and_binds_by_digest_not_name(server: FakeLlamaServer,
                                                                         model: tuple[Path, Path]) -> None:
    root, path = model
    profile = llama_profile(root, path, alias=ALIAS)
    target = RuntimeEndpoint(runtime_id="rt-llama", runtime_type=LLAMA_FAMILY, endpoint=server.url,
                             artifact_root=str(root))
    descriptor = describe_runtime(target, now=NOW)
    assert descriptor.health is RuntimeHealth.HEALTHY and descriptor.version == LLAMA_VERSION
    (listed,) = descriptor.models
    assert (listed.name, listed.digest) == (ALIAS, profile.artifact.manifest_digest)
    assert descriptor.limits == {"context_window": 8192, "parallel_slots": 1}
    assert [m.name for m in descriptor.loaded_models] == [ALIAS] and descriptor.loaded_models[0].context_length == 8192
    assert str(root) not in descriptor.model_dump_json() and str(path) not in descriptor.model_dump_json()
    observation = runtime_observation(descriptor)
    device = DeviceObservation(device_profile_id=profile.device.device_profile_id, state=CandidateState.AVAILABLE,
                               free_gpu_memory_mib=8000, available_ram_mib=30000, observed_at=NOW)
    binding = bind_live(profile, (observation,), device, now=NOW + timedelta(seconds=1), policy=BindingPolicy())
    assert binding.bindable and binding.runtime_ref == ALIAS and not binding.reasons
    assert binding.headroom is not None and binding.headroom.model_resident
    unrooted = describe_runtime(target.model_copy(update={"artifact_root": None}), now=NOW)
    assert unrooted.models[0].digest is None
    assert "MODEL_ARTIFACT_NOT_PRESENT" in bind_live(profile, (runtime_observation(unrooted),), device, now=NOW,
                                                     policy=BindingPolicy()).reasons
    other_build = observation.model_copy(update={"runtime_version": "6200+ffff"})
    assert "RUNTIME_VERSION_NOT_MEASURED" in bind_live(profile, (other_build,), device, now=NOW,
                                                       policy=BindingPolicy()).reasons


def test_runtime_probe_without_props_is_probe_failed_and_absent_server_is_unreachable(
        model: tuple[Path, Path]) -> None:
    root, path = model
    with FakeLlamaServer(alias=ALIAS, model_path=str(path), props_available=False) as fake:
        target = RuntimeEndpoint(runtime_id="rt-llama", runtime_type=LLAMA_FAMILY, endpoint=fake.url,
                                 artifact_root=str(root))
        descriptor = describe_runtime(target, now=NOW)
        assert descriptor.health is RuntimeHealth.PROBE_FAILED and descriptor.failure_code == "PROPS_HTTP_404"
        assert descriptor.models[0].digest is None and descriptor.version is None
        port = fake.server.server_port
    gone = describe_runtime(RuntimeEndpoint(runtime_id="rt-llama", runtime_type=LLAMA_FAMILY,
                                            endpoint=f"http://127.0.0.1:{port}"), now=NOW)
    assert gone.health in {RuntimeHealth.UNREACHABLE, RuntimeHealth.PROBE_FAILED} and not gone.available
