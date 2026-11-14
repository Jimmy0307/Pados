# SPDX-License-Identifier: AGPL-3.0-only
"""OP-009: every capability pack meets the cross-pack operability contract; violations are named, never silent."""
from __future__ import annotations

import pytest

from pados.capability.pack import CapabilityPackManifest, CheckerSpec, FailureCodeSpec
from pados.capability.pack_operability import adapter_health, check, profile
from pados.packs.document_intelligence import deterministic_stages as document_intelligence
from pados.packs.problem_solving import pack as problem_solving
from pados.packs.reference import pack as reference

# Every in-tree pack with a v1 manifest. A new pack adds itself here; its manifest must pass unchanged.
PACKS: dict[str, CapabilityPackManifest] = {
    "reference": reference.MANIFEST,
    "problem_solving": problem_solving.MANIFEST,
    # The model-backed manifests of this package are built per pin; tests/packs/document_intelligence checks them.
    "document_intelligence": document_intelligence.MANIFEST,
}


@pytest.mark.parametrize("name", sorted(PACKS))
def test_every_in_tree_pack_meets_the_operability_contract(name: str) -> None:
    assert check(PACKS[name]) == ()


@pytest.mark.parametrize("name", sorted(PACKS))
def test_the_profile_reads_the_sealed_manifest_and_claims_no_authority(name: str) -> None:
    manifest = PACKS[name]
    summary = profile(manifest)
    assert summary["manifest_digest"] == manifest.manifest_digest
    assert summary["timeout_seconds"] == manifest.runtime.timeout_seconds >= 1
    assert set(summary["retryable_failure_codes"]).isdisjoint(summary["terminal_failure_codes"])
    assert summary["semantic_authority"] == "NONE"


def test_problem_solving_declares_its_provider_outage_as_the_only_retryable_failure() -> None:
    assert profile(problem_solving.MANIFEST)["retryable_failure_codes"] == ["PS_PROVIDER_UNAVAILABLE"]


def _with(manifest: CapabilityPackManifest, **update: object) -> CapabilityPackManifest:
    # model_copy skips validation on purpose: the conformance check must catch what the schema alone allows.
    return manifest.model_copy(update=update)


@pytest.mark.parametrize(("codes", "finding"), [
    ((FailureCodeSpec(code="REF_INPUT_INVALID", retryable=True),), "RETRYABLE_REFUSAL:REF_INPUT_INVALID"),
    ((FailureCodeSpec(code="REF_SOURCE_UNAUTHORIZED", retryable=True),), "RETRYABLE_REFUSAL:REF_SOURCE_UNAUTHORIZED"),
    ((FailureCodeSpec(code="REF_DIGEST_STALE", retryable=True),), "RETRYABLE_REFUSAL:REF_DIGEST_STALE"),
    ((FailureCodeSpec(code="REF_PROVIDER_FAILED", retryable=True),), "RETRYABLE_CODE_NOT_TRANSIENT:REF_PROVIDER_FAILED"),
])
def test_a_retry_never_re_asks_a_refusal(codes: tuple[FailureCodeSpec, ...], finding: str) -> None:
    assert check(_with(reference.MANIFEST, failure_codes=codes)) == (finding,)


@pytest.mark.parametrize("code", ["REF_PROVIDER_TIMEOUT", "REF_NODE_UNAVAILABLE", "REF_QUEUE_BUSY",
                                  "REF_RATE_LIMITED"])
def test_transient_failures_may_be_retryable(code: str) -> None:
    assert check(_with(reference.MANIFEST, failure_codes=(FailureCodeSpec(code=code, retryable=True),))) == ()


def test_an_output_contract_without_a_checker_is_named() -> None:
    unchecked = _with(reference.MANIFEST, verification_hooks=())
    assert check(unchecked) == (f"OUTPUT_UNCHECKED:{reference.OUTPUT_CONTRACT}",)
    other = CheckerSpec(output_contract=reference.OUTPUT_CONTRACT, checker_id=reference.CHECKER_ID)
    assert check(_with(reference.MANIFEST, verification_hooks=(other,))) == ()


class _Raises:
    def available(self) -> bool:
        raise ConnectionError("provider down")


class _Truthy:
    def available(self) -> bool:
        return "yes"  # type: ignore[return-value]


def test_adapter_health_is_a_plain_bool_that_never_raises() -> None:
    assert adapter_health(reference.adapter()) == ()
    assert adapter_health(_Raises()) == ("HEALTH_RAISED:ConnectionError",)
    assert adapter_health(_Truthy()) == ("HEALTH_NOT_BOOL:str",)


def test_no_in_tree_v1_pack_is_missing_from_the_contract_list() -> None:
    from pathlib import Path

    packs_root = Path(__file__).resolve().parents[2] / "src" / "pados" / "packs"
    declaring = {path.parent.name for path in packs_root.rglob("*.py")
                 if "CapabilityPackManifest(" in path.read_text(encoding="utf-8")}
    assert declaring == set(PACKS)
