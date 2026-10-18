# SPDX-License-Identifier: AGPL-3.0-only
"""SessionBindingAuthority contract: identity, idempotency and no-rebind immutability (both implementations)."""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pados.capability_loop.binding_authority import (
    AUTHORITY_SECURITY_LEVEL,
    BindingAuthorityError,
    GovernedSessionBinding,
    SessionBindingAuthority,
)
from pados.capability_loop_runtime.binding import (
    InMemorySessionBindingAuthority,
    anchored_session_binding_authority,
)
from pados.enterprise.classification import DataClassification

PROVIDER = "openmaic.adaptive_learning"
SESSION = "job-123"


def _binding(*, project: str = "proj-a", work: str = "wc-1",
             classification: DataClassification = DataClassification.INTERNAL,
             gap: str = "gap:cad_edit") -> GovernedSessionBinding:
    return GovernedSessionBinding(
        provider_id=PROVIDER, external_session_ref=SESSION, project_id=project, work_contract_id=work,
        classification=classification, capability_gap_ref=gap,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"))


def _factories(tmp_path: Path) -> list[Callable[[], SessionBindingAuthority]]:
    return [
        InMemorySessionBindingAuthority,
        lambda: anchored_session_binding_authority(tmp_path / "log" / "binding.jsonl", tmp_path / "anchor"),
    ]


@pytest.fixture(params=[0, 1], ids=["in_memory", "file_anchored"])
def authority(request: pytest.FixtureRequest, tmp_path: Path) -> SessionBindingAuthority:
    return _factories(tmp_path)[request.param]()


def test_security_level_is_not_claimed_tamper_proof() -> None:
    assert AUTHORITY_SECURITY_LEVEL == "INDEPENDENTLY_ANCHORED_TAMPER_EVIDENT"
    assert "tamper_proof" not in AUTHORITY_SECURITY_LEVEL.lower()


def test_bind_then_resolve_returns_the_governed_context(authority: SessionBindingAuthority) -> None:
    authority.bind(_binding())
    resolved = authority.resolve(PROVIDER, SESSION)
    assert resolved is not None and resolved.project_id == "proj-a"
    assert resolved.work_contract_id == "wc-1" and resolved.capability_gap_ref == "gap:cad_edit"


def test_unknown_session_resolves_to_none(authority: SessionBindingAuthority) -> None:
    authority.bind(_binding())
    assert authority.resolve(PROVIDER, "job-unknown") is None
    assert authority.resolve("other.provider", SESSION) is None  # provider is part of the identity


def test_identical_rebind_is_idempotent(authority: SessionBindingAuthority) -> None:
    first = authority.bind(_binding())
    again = authority.bind(_binding())
    assert first.context_hash == again.context_hash
    assert authority.resolve(PROVIDER, SESSION) is not None


@pytest.mark.parametrize("field,value", [
    ("project", "proj-b"),
    ("work", "wc-2"),
    ("classification", DataClassification.RESTRICTED),
    ("gap", "gap:other"),
])
def test_rebind_to_a_different_context_is_refused(authority: SessionBindingAuthority, field: str,
                                                  value: object) -> None:
    authority.bind(_binding())
    with pytest.raises(BindingAuthorityError) as excinfo:
        authority.bind(_binding(**{field: value}))  # type: ignore[arg-type]
    assert excinfo.value.code == "BINDING_CONFLICT"
    # the original binding is unchanged
    assert authority.resolve(PROVIDER, SESSION).project_id == "proj-a"  # type: ignore[union-attr]
