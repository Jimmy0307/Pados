from datetime import UTC, datetime

import pytest
from li_fakes import NOW, RTX_616_QUERY, RTX_616_VERSION, CommandResult, FakeRunner, nvidia_query, session, vmware_host, windows_rtx_host

from pados.local_intelligence.audit import (
    ArchitectureFacts,
    BehaviorLabel,
    GateStatus,
    ModelCandidate,
    ModelLifecycleState,
    WeightsKind,
    fact,
    kv_cache_mib,
    license_audit,
    may_become_available,
    pre_download_gates,
    provenance_audit,
    size_fit_audit,
)
from pados.local_intelligence.device import DeviceProfiler
from pados.local_intelligence.probes import Basis

U = Basis.UPSTREAM_FACT
DIGEST = "sha256:" + "ab" * 32
ARCH = ArchitectureFacts(num_layers=36, num_kv_heads=8, head_dim=128, source="https://example.invalid/config.json")


def _rtx():
    runner = FakeRunner({nvidia_query(): CommandResult(0, RTX_616_QUERY),
                         "nvidia-smi --version": CommandResult(0, RTX_616_VERSION)})
    return DeviceProfiler(host=windows_rtx_host(), session=session(runner), clock=lambda: NOW).profile()


def _candidate(**updates):
    values = dict(
        candidate_id="example-8b", canonical_name=fact("Example-8B", U, "https://example.invalid/model"),
        publisher=fact("Example Lab", U, "https://example.invalid/org"),
        upstream_repository=fact("https://example.invalid/Example-8B", U, "https://example.invalid/Example-8B"),
        license_spdx=fact("Apache-2.0", U, "https://example.invalid/LICENSE"),
        license_url=fact("https://example.invalid/LICENSE", U, "https://example.invalid/LICENSE"),
        commercial_use=fact(True, U, "https://example.invalid/LICENSE"),
        weights_kind=WeightsKind.QUANTIZED_FROM_ORIGINAL, weights_kind_basis=U,
        distributor=fact("registry.ollama.ai/library", U, "https://registry.ollama.ai/v2/library/example/manifests/8b"),
        gguf_provenance=fact("Ollama library quantization of the publisher's weights", U, "https://ollama.com/library/example"),
        model_layer_digest=fact(DIGEST, U), manifest_digest=fact("sha256:" + "cd" * 32, U),
        license_layer_digest=fact("sha256:" + "ef" * 32, U), model_layer_size_bytes=fact(5_200_000_000, U),
        kv_architecture=ARCH, retrieved_at=datetime(2026, 10, 7, tzinfo=UTC))
    values.update(updates)
    return ModelCandidate(**values)


def test_an_apache_original_quantization_that_fits_passes_all_three_gates():
    results = pre_download_gates(_candidate(), _rtx())
    assert [r.status for r in results] == [GateStatus.PASS] * 3 and may_become_available(results)
    figures = results[2].figures
    assert figures["kv_cache_mib"] == 1152 and figures["required_mib"] == 4960 + 1152 + 768
    assert figures["vram_free_mib"] == 7891 and "MEASURED_FREE_VRAM_OK" in results[2].reasons


def test_custom_licenses_are_never_an_automatic_pass():
    result = license_audit(_candidate(license_spdx=fact("LicenseRef-Gemma-Terms", U, "https://example.invalid/terms")))
    assert result.status is GateStatus.REVIEW_REQUIRED


@pytest.mark.parametrize("update", [
    {"license_spdx": fact("Apache-2.0", Basis.MODEL_CARD_CLAIM)},
    {"license_layer_digest": fact(None, Basis.UNKNOWN)},
    {"commercial_use": fact(False, U)},
])
def test_license_gate_fails_without_upstream_evidence(update):
    assert license_audit(_candidate(**update)).status is GateStatus.FAIL


@pytest.mark.parametrize("update", [
    {"distributor": fact("hf.co/someone/example-gguf", U)},
    {"weights_kind": WeightsKind.MERGED},
    {"weights_kind": WeightsKind.ABLITERATED},
    {"weights_kind_basis": Basis.COMMUNITY_CLAIM},
    {"model_layer_digest": fact("not-a-digest", U)},
    {"publisher": fact("Example Lab", Basis.COMMUNITY_CLAIM)},
])
def test_provenance_gate_fails_closed(update):
    assert provenance_audit(_candidate(**update)).status is GateStatus.FAIL


def test_size_gate_uses_measured_free_vram_and_context():
    too_big = size_fit_audit(_candidate(model_layer_size_bytes=fact(8_988_124_069, U)), _rtx())
    assert too_big.status is GateStatus.FAIL and "MEASURED_FREE_VRAM_INSUFFICIENT" in too_big.reasons
    at_16k = size_fit_audit(_candidate(), _rtx(), context_tokens=16384)
    assert at_16k.figures["kv_cache_mib"] == 2304 and at_16k.status is GateStatus.FAIL  # f16 KV at 16K does not fit
    assert kv_cache_mib(ARCH, 8192, bytes_per_value=1) == 576


def test_size_gate_fails_on_a_device_without_a_measured_gpu():
    device = DeviceProfiler(host=vmware_host(), session=session(FakeRunner()), clock=lambda: NOW).profile()
    result = size_fit_audit(_candidate(), device)
    assert result.status is GateStatus.FAIL and result.reasons == ("DEVICE_GPU_ABSENT",)


def test_behaviour_labels_never_change_a_gate_result():
    plain = pre_download_gates(_candidate(), _rtx())
    labelled = pre_download_gates(_candidate(behavior_labels=tuple(BehaviorLabel)), _rtx())
    assert [(r.status, r.reasons) for r in plain] == [(r.status, r.reasons) for r in labelled]


def test_lifecycle_states_are_distinct_and_audit_starts_at_audited():
    assert len({state.value for state in ModelLifecycleState}) == 11
    assert _candidate().state is ModelLifecycleState.AUDITED
