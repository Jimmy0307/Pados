# SPDX-License-Identifier: AGPL-3.0-only
"""Stage output verification: evidence about a candidate, never acceptance.

* SCHEMA               - an output-contract validator registered by the operator checks the output bytes;
* DETERMINISTIC_REPLAY - the stage is run again under the same binding and must yield identical hashes;
* REFERENCE_CHECK      - an operator-registered hook compares outputs with a trusted reference/fixture;
* NONE                 - the output stays NOT_VERIFIED.

HUMAN_REVIEW and UNIT_TESTS happen outside the fabric (existing governed paths), so here they never
yield PASSED. A declared automatic method the fabric cannot perform (no validator, no replay) makes the
result NOT_VERIFIED rather than PASSED. ``VerificationResult`` has no acceptance field: acceptance only
happens through the existing receipt -> validation -> CAS path.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from pados.capability.adapter import StageOutput
from pados.capability.artifacts import ArtifactRef, ArtifactStore
from pados.capability.model import CapabilityDescriptor, Determinism, VerificationMethod
from pados.capability.receipts import VerificationStatus
from pados.execution.context import canonical_hash

VERIFIER_ID = "pados.capability.verifier.v1"
MAX_VALIDATORS = 512
_CONTRACT_REF = re.compile(r"^[a-z][a-z0-9_.-]{2,79}/v[0-9]{1,3}$")
_VERIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_AUTOMATIC = (VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY, VerificationMethod.REFERENCE_CHECK)

Outputs = Sequence[tuple[ArtifactRef, bytes]]


class OutputValidator(Protocol):
    def __call__(self, outputs: Outputs) -> bool: ...


class VerificationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MethodResult:
    method: VerificationMethod
    status: VerificationStatus
    code: str
    checker_id: str


@dataclass(frozen=True)
class VerificationResult:
    method: VerificationMethod  # the first method that produced evidence (or the first declared one)
    status: VerificationStatus
    verifier_id: str | None
    evidence_hash: str | None
    results: tuple[MethodResult, ...]


class ValidatorRegistry:
    """Per output-contract checkers (SCHEMA validators or REFERENCE_CHECK hooks); no silent override."""

    def __init__(self) -> None:
        self._checks: dict[str, tuple[str, OutputValidator]] = {}

    def register(self, output_contract: str, checker_id: str, validator: OutputValidator) -> None:
        if not _CONTRACT_REF.fullmatch(output_contract) or not _VERIFIER.fullmatch(checker_id):
            raise VerificationError("VALIDATOR_IDENTITY_INVALID")
        if not callable(validator):
            raise VerificationError("VALIDATOR_NOT_CALLABLE")
        if output_contract in self._checks:
            raise VerificationError("VALIDATOR_ALREADY_REGISTERED")
        if len(self._checks) >= MAX_VALIDATORS:
            raise VerificationError("VALIDATOR_REGISTRY_FULL")
        self._checks[output_contract] = (checker_id, validator)

    def get(self, output_contract: str) -> tuple[str, OutputValidator] | None:
        return self._checks.get(output_contract)


class VerificationEngine:
    def __init__(self, *, schemas: ValidatorRegistry | None = None,
                 references: ValidatorRegistry | None = None) -> None:
        self._schemas = schemas or ValidatorRegistry()
        self._references = references or ValidatorRegistry()

    def verify(self, capability: CapabilityDescriptor, output: StageOutput, store: ArtifactStore, *,
               seed: int | None, replay: Callable[[], StageOutput] | None,
               adapter_determinism: Determinism | None = None) -> VerificationResult:
        """``adapter_determinism`` is the SELECTED adapter's digest-bound declaration. A capability's determinism
        is the weakest any adapter may declare, so a DETERMINISTIC adapter of a PROBABILISTIC capability is still
        replayed (a replay that differs fails closed as REPLAY_MISMATCH). Replay is a repeatability check and cannot
        prove the declaration: a stateful, cached or falsely declared adapter can return identical outputs twice, and
        a match never establishes factual correctness. It never weakens a stronger capability."""
        declared = capability.verification
        if VerificationMethod.NONE in declared:
            return VerificationResult(VerificationMethod.NONE, VerificationStatus.NOT_VERIFIED, None, None, ())
        if output.partial:
            return self._finish(declared[0], output, (MethodResult(declared[0], VerificationStatus.NOT_VERIFIED,
                                                                   "PARTIAL_OUTPUT_NOT_VERIFIED", VERIFIER_ID),))
        results: list[MethodResult] = []
        for method in declared:
            if method is VerificationMethod.SCHEMA:
                results.append(self._check(method, self._schemas.get(capability.output_contract), output, store))
            elif method is VerificationMethod.REFERENCE_CHECK:
                results.append(self._check(method, self._references.get(capability.output_contract), output, store))
            elif method is VerificationMethod.DETERMINISTIC_REPLAY:
                results.append(self._replay(capability, output, seed, replay, adapter_determinism))
            else:  # HUMAN_REVIEW / UNIT_TESTS: performed by existing governed paths, not here
                results.append(MethodResult(method, VerificationStatus.NOT_VERIFIED, "EXTERNAL_METHOD", VERIFIER_ID))
        return self._finish(declared[0], output, tuple(results))

    @staticmethod
    def _check(method: VerificationMethod, entry: tuple[str, OutputValidator] | None, output: StageOutput,
               store: ArtifactStore) -> MethodResult:
        if entry is None:
            return MethodResult(method, VerificationStatus.NOT_VERIFIED, f"{method.value}_CHECKER_MISSING",
                                VERIFIER_ID)
        checker_id, validator = entry
        try:
            loaded = [(ref, store.get(ref)) for ref in output.outputs]
            ok = validator(tuple(loaded)) is True
        except Exception:  # noqa: BLE001 - a crashing validator is a failed check, never a pass
            ok = False
        status = VerificationStatus.PASSED if ok else VerificationStatus.FAILED
        return MethodResult(method, status, f"{method.value}_{status.value}", checker_id)

    @staticmethod
    def _replay(capability: CapabilityDescriptor, output: StageOutput, seed: int | None,
                replay: Callable[[], StageOutput] | None,
                adapter_determinism: Determinism | None = None) -> MethodResult:
        method = VerificationMethod.DETERMINISTIC_REPLAY
        replayable = any(
            determinism is Determinism.DETERMINISTIC or (determinism is Determinism.SEEDED and seed is not None)
            for determinism in (capability.determinism, adapter_determinism))
        if not replayable or replay is None:
            return MethodResult(method, VerificationStatus.NOT_VERIFIED, "REPLAY_NOT_APPLICABLE", VERIFIER_ID)
        try:
            again = replay()
        except Exception:  # noqa: BLE001 - a replay that cannot run is a failed replay
            return MethodResult(method, VerificationStatus.FAILED, "REPLAY_FAILED", VERIFIER_ID)
        same = [ref.sha256 for ref in again.outputs] == [ref.sha256 for ref in output.outputs] and not again.partial
        status = VerificationStatus.PASSED if same else VerificationStatus.FAILED
        return MethodResult(method, status, "REPLAY_MATCH" if same else "REPLAY_MISMATCH", VERIFIER_ID)

    @staticmethod
    def _finish(first: VerificationMethod, output: StageOutput,
                results: tuple[MethodResult, ...]) -> VerificationResult:
        automatic = [item for item in results if item.method in _AUTOMATIC]
        if any(item.status is VerificationStatus.FAILED for item in results):
            status = VerificationStatus.FAILED
        elif automatic and all(item.status is VerificationStatus.PASSED for item in automatic):
            status = VerificationStatus.PASSED
        else:
            status = VerificationStatus.NOT_VERIFIED
        performed = next((item.method for item in results if item.status is not VerificationStatus.NOT_VERIFIED),
                         first)
        evidence = canonical_hash({
            "verifier_id": VERIFIER_ID,
            "outputs": [ref.sha256 for ref in output.outputs],
            "results": [[item.method.value, item.status.value, item.code, item.checker_id] for item in results],
        })
        return VerificationResult(performed, status, VERIFIER_ID, evidence, results)


def results_document(result: VerificationResult) -> Mapping[str, object]:
    """Content-free summary (methods, statuses, codes, checker ids) for reports."""
    return {"method": result.method.value, "status": result.status.value, "verifier_id": result.verifier_id,
            "evidence_hash": result.evidence_hash,
            "results": [[item.method.value, item.status.value, item.code, item.checker_id] for item in result.results]}
