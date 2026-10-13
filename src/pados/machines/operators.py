# SPDX-License-Identifier: AGPL-3.0-only
"""Operator authentication for privileged grants (no caller-constructed actor is proof).

The operator registry is an owner-only JSON file mapping ``operator_id`` to a
SecretRef for that operator's HMAC proof key; keys are resolved only through
``SecretBoundary`` and never logged or stored. For every grant PADOS issues a
fresh ``Challenge`` bound to (request digest, machine_id, project_id, expiry)
and MAC'd with the Home Base key, so a challenge cannot be invented. The
requester and the approver each answer with an HMAC proof over that challenge
and their role; ``OperatorAuthenticator.verify`` checks the challenge MAC,
expiry, request binding, registration and the proof in constant time.

``LocalOperator`` is the *verified result* of that check. Python cannot make a
class truly unconstructible, so a forged ``LocalOperator`` is useless by
design: ``issue_grant`` takes the raw ``OperatorProof``s and re-verifies them
itself, and the ledger records only operator ids and proof digests.
"""
from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from pados.enterprise.secrets import SecretResolver, is_secret_ref
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.machines.errors import MachineError
from pados.machines.fsguard import read_owner_only_json
from pados.machines.identity import MACHINE_ID_PATTERN
from pados.machines.inventory import utc_now
from pados.machines.keys import SecretKey, digest, mac_with
from pados.machines.requests import IDENTIFIER, GrantRequest

OPERATOR_REGISTRY_SCHEMA = "pados.machines.operators.v1"
CHALLENGE_DOMAIN = "pados-privileged-grant-challenge-v1"
PROOF_DOMAIN = "pados-privileged-grant-proof-v1"
MAX_CHALLENGE_LIFETIME = timedelta(minutes=5)
_REGISTRY_KEYS = frozenset({"schema_version", "operators"})
_OPERATOR_KEYS = frozenset({"operator_id", "proof_key_ref"})
_HEX64 = r"^[0-9a-f]{64}$"


class ProofRole(StrEnum):
    REQUESTER = "REQUESTER"
    APPROVER = "APPROVER"


class Challenge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    challenge_id: str = Field(pattern=r"^ch-[0-9a-f]{32}$")
    request_digest: str = Field(pattern=_HEX64)
    machine_id: str = Field(pattern=MACHINE_ID_PATTERN)
    project_id: str
    issued_at: AwareDatetime
    expires_at: AwareDatetime
    challenge_mac: str = Field(pattern=_HEX64)

    def body(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"challenge_mac"})

    def proof_payload(self, role: ProofRole, operator_id: str) -> dict[str, Any]:
        return {"challenge_id": self.challenge_id, "role": role.value, "operator_id": operator_id,
                "request_digest": self.request_digest, "machine_id": self.machine_id,
                "project_id": self.project_id, "expires_at": self.expires_at.isoformat()}


@dataclass(frozen=True)
class OperatorProof:
    """What an operator presents: their id and an HMAC proof over a PADOS challenge (raw input)."""

    operator_id: str
    proof: str


@dataclass(frozen=True)
class LocalOperator:
    """Verified result of ``OperatorAuthenticator.verify`` (never proof by itself; see module doc)."""

    actor_id: str
    role: ProofRole
    challenge_id: str
    proof_digest: str


def compute_proof(key: str, challenge: Challenge, role: ProofRole, operator_id: str) -> str:
    """Operator-side proof computation (for the operator's own tooling; PADOS never holds this key)."""
    return mac_with(key.encode("utf-8"), PROOF_DOMAIN, challenge.proof_payload(role, operator_id))


class OperatorAuthenticator:
    def __init__(self, registry_path: str | Path, *, home_key: SecretKey,
                 resolver: SecretResolver | None = None, root: str | Path | None = None) -> None:
        self.registry_path = Path(registry_path)
        self.root = Path(root) if root is not None else None
        self._home_key = home_key
        self._resolver = resolver

    def _registry(self) -> dict[str, str]:
        data = read_owner_only_json(self.registry_path, "OPERATOR_REGISTRY", self.root)
        try:
            if not isinstance(data, dict) or data.get("schema_version") != OPERATOR_REGISTRY_SCHEMA:
                raise MachineError("OPERATOR_REGISTRY_INVALID")
            assert_minimized(data, allowed_keys=_REGISTRY_KEYS)
            rows = data.get("operators")
            if not isinstance(rows, list):
                raise MachineError("OPERATOR_REGISTRY_INVALID")
            operators: dict[str, str] = {}
            for row in rows:
                if not isinstance(row, dict) or set(row) != _OPERATOR_KEYS:
                    raise MachineError("OPERATOR_REGISTRY_INVALID")
                operator_id, ref = row["operator_id"], row["proof_key_ref"]
                if not isinstance(operator_id, str) or not IDENTIFIER.fullmatch(operator_id) \
                        or not is_secret_ref(ref) or operator_id in operators:
                    raise MachineError("OPERATOR_REGISTRY_INVALID")
                operators[operator_id] = str(ref)
        except DataMinimizationViolation:
            raise MachineError("OPERATOR_REGISTRY_INVALID") from None
        return operators

    def challenge(self, request: GrantRequest, *, now: datetime | None = None,
                  ttl: timedelta = timedelta(minutes=2)) -> Challenge:
        """A fresh, PADOS-MAC'd challenge bound to ``request``."""
        if not timedelta(0) < ttl <= MAX_CHALLENGE_LIFETIME:
            raise MachineError("CHALLENGE_LIFETIME_INVALID")
        issued = now or utc_now()
        body: dict[str, Any] = {
            "challenge_id": "ch-" + secrets.token_hex(16), "request_digest": request.digest,
            "machine_id": request.machine_id, "project_id": request.project_id,
            "issued_at": issued.isoformat(), "expires_at": (issued + ttl).isoformat(),
        }
        unsigned = Challenge.model_validate({**body, "challenge_mac": "0" * 64})
        # MAC over the canonical serialized form, exactly what ``verify`` recomputes.
        return unsigned.model_copy(update={"challenge_mac": self._home_key.mac(CHALLENGE_DOMAIN, unsigned.body())})

    def verify(self, proof: object, *, role: ProofRole, challenge: Challenge, request: GrantRequest,
               now: datetime | None = None) -> LocalOperator:
        """Verify one operator's proof; every failure is a code."""
        at = now or utc_now()
        if not isinstance(proof, OperatorProof) or not isinstance(challenge, Challenge):
            raise MachineError("OPERATOR_PROOF_REQUIRED")
        if not self._home_key.verify(CHALLENGE_DOMAIN, challenge.body(), challenge.challenge_mac):
            raise MachineError("CHALLENGE_INVALID")
        if (challenge.request_digest, challenge.machine_id, challenge.project_id) != (
                request.digest, request.machine_id, request.project_id):
            raise MachineError("CHALLENGE_REQUEST_MISMATCH")
        if not challenge.issued_at <= at < challenge.expires_at:
            raise MachineError("CHALLENGE_EXPIRED")
        ref = self._registry().get(proof.operator_id) if isinstance(proof.operator_id, str) else None
        if ref is None:
            raise MachineError("OPERATOR_UNKNOWN")
        key = SecretKey(ref, resolver=self._resolver, prefix="OPERATOR_KEY")
        expected = key.mac(PROOF_DOMAIN, challenge.proof_payload(role, proof.operator_id))
        if not isinstance(proof.proof, str) or not hmac.compare_digest(expected, proof.proof):
            raise MachineError("OPERATOR_PROOF_INVALID")
        return LocalOperator(proof.operator_id, role, challenge.challenge_id, digest(proof.proof))
