# SPDX-License-Identifier: AGPL-3.0-only
"""Cross-pack operability contract (OP-009): what every capability pack must declare to be operable.

A pack manifest already carries the operability facts, and they are sealed in its digest:
- timeout: ``runtime.timeout_seconds``, bounded by the schema;
- resources: ``runtime.device`` and each adapter's ``resources``;
- network and egress: ``runtime.network`` and ``security.egress``;
- retry classification: ``failure_codes[].retryable``;
- evidence: ``evidence_requirements``, which always include the core keys.

``check`` adds the cross-pack rules the schema does not express on its own:
- every output contract has a deterministic checker;
- a retryable failure code is transient-shaped (timeout, unavailable, busy, ...);
- no refusal-shaped code (invalid, unauthorized, stale, ...) is retryable, so a retry never re-asks a refusal.

``profile`` is the operator-facing summary. Health for an in-process pack is the governed adapter's ``available()``.
That method returns a bool and never raises; ``adapter_health`` checks that. This module reads manifests only and
grants nothing: registration, grants and the executor stay authoritative.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Protocol

from pados.capability.pack import CapabilityPackManifest

# Refusal tokens take precedence: a code that says the input or the caller is wrong must never be retried.
_REFUSAL = re.compile(r"(?:^|_)(INVALID|MALFORMED|UNAUTHORIZED|FORBIDDEN|DENIED|REFUSED|MISMATCH|CONFLICT|STALE|"
                      r"UNKNOWN|EMPTY|POLICY|APPROVAL|NOT_ALLOWED|TOO_LARGE)(?:_|$)")
_TRANSIENT = re.compile(r"(?:^|_)(TIMEOUT|TIMED_OUT|UNAVAILABLE|OFFLINE|BUSY|THROTTLED|RATE_LIMITED|EXHAUSTED|"
                        r"TRANSIENT|UNREACHABLE)(?:_|$)")


class _Available(Protocol):
    def available(self) -> bool: ...


def check(manifest: CapabilityPackManifest) -> tuple[str, ...]:
    """Each finding is ``RULE:subject``; an empty tuple means the pack meets the operability contract."""
    findings: list[str] = []
    checked = {hook.output_contract for hook in manifest.verification_hooks}
    findings += [f"OUTPUT_UNCHECKED:{capability.output_contract}" for capability in manifest.capabilities
                 if capability.output_contract not in checked]
    for failure in manifest.failure_codes:
        if failure.retryable and _REFUSAL.search(failure.code):
            findings.append(f"RETRYABLE_REFUSAL:{failure.code}")
        elif failure.retryable and not _TRANSIENT.search(failure.code):
            findings.append(f"RETRYABLE_CODE_NOT_TRANSIENT:{failure.code}")
    return tuple(findings)


def profile(manifest: CapabilityPackManifest) -> Mapping[str, Any]:
    """The operability facts an operator needs, read from the sealed manifest (no new authority)."""
    return {
        "pack_id": manifest.pack_id,
        "version": manifest.version,
        "manifest_digest": manifest.manifest_digest,
        "runtime_type": manifest.runtime.runtime_type.value,
        "timeout_seconds": manifest.runtime.timeout_seconds,
        "network": manifest.runtime.network.value,
        "egress": manifest.security.egress.value,
        "device": [requirement.model_dump(mode="json") for requirement in manifest.runtime.device],
        "retryable_failure_codes": sorted(f.code for f in manifest.failure_codes if f.retryable),
        "terminal_failure_codes": sorted(f.code for f in manifest.failure_codes if not f.retryable),
        "evidence_requirements": list(manifest.evidence_requirements),
        "checked_output_contracts": sorted(hook.output_contract for hook in manifest.verification_hooks),
        "health": "governed adapter available()",
        "semantic_authority": "NONE",
    }


def adapter_health(adapter: _Available) -> tuple[str, ...]:
    """Health must be a plain bool and must never raise; anything else is a finding."""
    try:
        value = adapter.available()
    except Exception as exc:  # noqa: BLE001 - the contract is exactly that health never raises
        return (f"HEALTH_RAISED:{type(exc).__name__}",)
    return () if isinstance(value, bool) else (f"HEALTH_NOT_BOOL:{type(value).__name__}",)
