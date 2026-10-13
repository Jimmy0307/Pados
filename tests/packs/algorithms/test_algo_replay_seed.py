"""Replay binds the TRUSTED request seed (CR-14b N-01).

A trusted binding or receipt that carries a seed describes a request the adapter refuses
(``ALGORITHM_SEED_NOT_APPLICABLE``: the algorithms are DETERMINISTIC). Before this fix the seed was dropped
when ``ExpectedStage`` was built, so a record with ``seed = None`` and correctly recomputed bytes replayed
as a MATCH for that binding. A seed the caller did not establish fails closed.
"""
from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from _algo_harness import policy, put_json, run_bound, students
from pados.capability.adapter import CapabilityAdapterError, StageRequest, bind_stage, run_bound_stage
from pados.capability.artifacts import ObjectStoreArtifacts
from pados.capability.model import Determinism
from pados.capability.receipts import StageReceipt, VerificationStatus, parameters_hash
from pados.enterprise.classification import DataClassification
from pados.packs.algorithms import AlgorithmAdapter, ExpectedStage, verify_replay
from pados.packs.algorithms.registry import DRSA


class Stage:
    """A real, seedless run: its record has ``seed = None`` and its bytes recompute exactly."""

    def __init__(self, tmp_path: Path) -> None:
        self.store = ObjectStoreArtifacts(tmp_path / "objects")
        self.adapter = AlgorithmAdapter(DRSA)
        self.source = put_json(self.store, students())
        self.binding, self.output = run_bound(self.store, DRSA.capability, self.adapter, [self.source], {})
        result_ref, record_ref = self.output.outputs
        self.result, self.record = self.store.get(result_ref), self.store.get(record_ref)
        self.dataset = self.store.get(self.source)

    def seeded_binding(self, seed: int) -> Any:
        """The executor's own binding for the same request, but with a trusted seed."""
        ctx, decision, self.scope = policy(DRSA.capability, self.adapter)
        request = StageRequest("s1", DRSA.capability.capability_id, (self.source,), {},
                               DataClassification.CONFIDENTIAL, seed=seed)
        return bind_stage(DRSA.capability, self.adapter.descriptor, ctx, decision, request,
                          permitted_inputs=frozenset({self.source.sha256}))

    def receipt(self, seed: int | None, determinism: Determinism = Determinism.DETERMINISTIC) -> StageReceipt:
        descriptor = self.adapter.descriptor
        return StageReceipt(
            plan_id="pl", stage_id="s1", attempt_id="a1", capability_id=DRSA.capability.capability_id,
            capability_family=DRSA.capability.family, capability_version=DRSA.capability.version,
            capability_digest=self.binding.capability_digest, adapter_id=descriptor.adapter_id,
            adapter_version=descriptor.version, adapter_digest=self.binding.adapter_digest,
            resource_id="local-cpu", determinism=determinism, inputs=(self.source,), outputs=self.output.outputs,
            parameters_hash=parameters_hash({}), seed=seed, started_at="2026-10-03T00:00:00+00:00",
            finished_at="2026-10-03T00:00:01+00:00", duration_ms=1000,
            verification_method=DRSA.capability.verification[1], verification=VerificationStatus.NOT_VERIFIED,
            policy_decision_hash="b" * 64, prev_hash="0" * 64)

    def verify(self, expected: ExpectedStage) -> Any:
        return verify_replay(expected, self.record, self.result, self.dataset, {})


def test_trusted_binding_seed_is_carried_into_the_expected_stage(tmp_path: Path) -> None:
    stage = Stage(tmp_path)
    assert "seed" in {f.name for f in fields(ExpectedStage)}
    assert ExpectedStage.from_binding(stage.binding, stage.output).seed is None
    assert ExpectedStage.from_binding(stage.seeded_binding(123), stage.output).seed == 123


def test_seeded_binding_with_a_seedless_record_and_correct_bytes_is_refused(tmp_path: Path) -> None:
    stage = Stage(tmp_path)
    seeded = stage.seeded_binding(123)
    # Execution refuses this exact binding ...
    with pytest.raises(CapabilityAdapterError) as refused:
        run_bound_stage(seeded, stage.adapter, stage.store, revalidate=lambda: stage.scope)
    assert refused.value.code == "ALGORITHM_SEED_NOT_APPLICABLE"
    # ... so replay must not certify bytes for it, even though record (seed None) and bytes are correct.
    verdict = stage.verify(ExpectedStage.from_binding(seeded, stage.output))
    assert verdict.code == "REPLAY_BINDING_SEED_NOT_APPLICABLE"
    assert verdict.status is VerificationStatus.FAILED and not verdict.passed
    # The same bytes under the seedless binding still match.
    assert stage.verify(ExpectedStage.from_binding(stage.binding, stage.output)).code == "REPLAY_MATCH"


@pytest.mark.parametrize("determinism", [Determinism.DETERMINISTIC, Determinism.SEEDED])
def test_seeded_receipt_with_a_seedless_record_and_correct_bytes_is_refused(tmp_path: Path,
                                                                              determinism: Determinism) -> None:
    stage = Stage(tmp_path)
    verdict = stage.verify(ExpectedStage.from_receipt(stage.receipt(123, determinism)))
    assert verdict.code == "REPLAY_BINDING_SEED_NOT_APPLICABLE" and not verdict.passed
    assert stage.verify(ExpectedStage.from_receipt(stage.receipt(None))).code == "REPLAY_MATCH"


def test_a_seed_the_caller_did_not_establish_fails_closed(tmp_path: Path) -> None:
    """Hand-built (positional, pre-N-01 shape) ExpectedStage: the seed is unknown, so replay refuses."""
    stage = Stage(tmp_path)
    good = ExpectedStage.from_binding(stage.binding, stage.output)
    legacy = ExpectedStage(good.capability_id, good.capability_digest, good.adapter_id, good.adapter_version,
                           good.adapter_digest, good.request_parameters_hash, good.inputs, good.outputs)
    verdict = stage.verify(legacy)
    assert verdict.code == "REPLAY_BINDING_SEED_UNKNOWN" and not verdict.passed
    explicit = ExpectedStage(good.capability_id, good.capability_digest, good.adapter_id, good.adapter_version,
                             good.adapter_digest, good.request_parameters_hash, good.inputs, good.outputs, seed=None)
    assert stage.verify(explicit).code == "REPLAY_MATCH"
    for bogus in (0, True, "123"):  # anything but an established None is refused
        forged: Any = bogus
        verdict = stage.verify(ExpectedStage(*(getattr(good, f.name) for f in fields(good)[:-1]), seed=forged))
        assert verdict.code == "REPLAY_BINDING_SEED_NOT_APPLICABLE", bogus
