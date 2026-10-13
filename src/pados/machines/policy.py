# SPDX-License-Identifier: AGPL-3.0-only
"""Operator credential policy: the inventory alone never activates a credential.

The machine inventory says *which* reference a machine profile uses; it is not
permission to use it. Credential use is authorized only by a separate,
owner-only policy file the operator writes::

    {"schema_version": "pados.machines.credential-policy.v1",
     "machines": {"mach-...": {"username": "<account>",
                               "allowed_auth_refs": ["secretref://...", "ssh-agent://default"],
                               "allowed_agent_key_fingerprints": ["SHA256:..."],
                               "agent_public_keys": ["ssh-ed25519 AAAA..."]}}}

A reference is resolved only if the policy allows it for that ``machine_id``
*and* login ``username``. ssh-agent use additionally requires pinned agent
keys: every ``agent_public_keys`` entry must have an allowed fingerprint, and
the probe restricts the agent to exactly those keys (``IdentitiesOnly=yes`` +
``IdentityFile=<public key file>``). Anything else is a code
(``CREDENTIAL_POLICY_*``), never a silent attempt. Keep this file out of CI and
of any process that must not authenticate to machines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.machines.bootstrap import canonical_public_key
from pados.machines.errors import MachineError
from pados.machines.fsguard import read_owner_only_json
from pados.machines.identity import FINGERPRINT_PATTERN, HostKey, is_machine_id
from pados.machines.profile import AUTH_REF_PATTERN, USERNAME_PATTERN, AuthMethod, MachineProfile, validated

CREDENTIAL_POLICY_SCHEMA = "pados.machines.credential-policy.v1"
CREDENTIAL_POLICY_NAME = "machine_credential_policy.json"
_POLICY_KEYS = frozenset({"schema_version", "machines"})
_RULE_KEYS = frozenset({"username", "allowed_auth_refs", "allowed_agent_key_fingerprints", "agent_public_keys"})


class MachineCredentialRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    username: str = Field(pattern=USERNAME_PATTERN)
    allowed_auth_refs: tuple[str, ...] = Field(default=(), max_length=16)
    allowed_agent_key_fingerprints: tuple[str, ...] = Field(default=(), max_length=16)
    agent_public_keys: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("allowed_auth_refs")
    @classmethod
    def references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not all(re.fullmatch(AUTH_REF_PATTERN, ref) for ref in value):
            raise MachineError("CREDENTIAL_POLICY_INVALID")
        return value

    @field_validator("allowed_agent_key_fingerprints")
    @classmethod
    def fingerprints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not all(re.fullmatch(FINGERPRINT_PATTERN, item) for item in value):
            raise MachineError("CREDENTIAL_POLICY_INVALID")
        return value

    @field_validator("agent_public_keys")
    @classmethod
    def public_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(canonical_public_key(item) for item in value)

    @model_validator(mode="after")
    def pinned(self) -> MachineCredentialRule:
        allowed = set(self.allowed_agent_key_fingerprints)
        if any(agent_key_fingerprint(key) not in allowed for key in self.agent_public_keys):
            raise MachineError("CREDENTIAL_POLICY_AGENT_KEY_NOT_ALLOWED")
        return self


def agent_key_fingerprint(public_key: str) -> str:
    parts = public_key.split()
    return HostKey.from_openssh(f"{parts[0]} {parts[1]}").fingerprint


@dataclass(frozen=True)
class CredentialDecision:
    """What the probe may use for one connection: the reference and, for agents, the pinned keys."""

    auth_ref: str
    agent_public_keys: tuple[str, ...] = ()


class CredentialPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = CREDENTIAL_POLICY_SCHEMA
    machines: dict[str, MachineCredentialRule] = Field(default_factory=dict)

    @model_validator(mode="after")
    def keyed_by_machine(self) -> CredentialPolicy:
        if self.schema_version != CREDENTIAL_POLICY_SCHEMA or not all(is_machine_id(k) for k in self.machines):
            raise MachineError("CREDENTIAL_POLICY_INVALID")
        return self

    @classmethod
    def load(cls, path: str | Path, *, root: str | Path | None = None) -> CredentialPolicy:
        """Load the owner-only policy; with ``root`` every ancestor up to it is checked too."""
        data = read_owner_only_json(Path(path), "CREDENTIAL_POLICY", Path(root) if root is not None else None)
        code: str | None = None
        try:
            if not isinstance(data, dict) or not isinstance(data.get("machines"), dict):
                raise MachineError("CREDENTIAL_POLICY_INVALID")
            assert_minimized({k: v for k, v in data.items() if k != "machines"}, allowed_keys=_POLICY_KEYS)
            for rule in data["machines"].values():
                if not isinstance(rule, dict) or set(rule) - _RULE_KEYS:
                    raise MachineError("CREDENTIAL_POLICY_INVALID")
                assert_minimized(rule, allowed_keys=_RULE_KEYS)
            return validated(cls, data, prefix="CREDENTIAL_POLICY")
        except MachineError as exc:
            code = exc.code
        except DataMinimizationViolation:
            code = "CREDENTIAL_POLICY_INVALID"
        raise MachineError(code if code.startswith("CREDENTIAL_POLICY") else "CREDENTIAL_POLICY_INVALID")

    def decide(self, profile: MachineProfile) -> CredentialDecision:
        """The credential the probe may use for ``profile``, or a ``CREDENTIAL_POLICY_*`` code."""
        rule = self.machines.get(profile.machine_id)
        if rule is None:
            raise MachineError("CREDENTIAL_POLICY_NO_RULE", machine_id=profile.machine_id)
        if rule.username != profile.username:
            raise MachineError("CREDENTIAL_POLICY_USERNAME_MISMATCH", machine_id=profile.machine_id)
        if profile.auth_ref not in rule.allowed_auth_refs:
            raise MachineError("CREDENTIAL_POLICY_REF_NOT_ALLOWED", machine_id=profile.machine_id)
        if profile.effective_auth_method is AuthMethod.SSH_AGENT:
            if not rule.agent_public_keys:
                raise MachineError("CREDENTIAL_POLICY_AGENT_KEY_NOT_PINNED", machine_id=profile.machine_id)
            return CredentialDecision(profile.auth_ref, rule.agent_public_keys)
        return CredentialDecision(profile.auth_ref)


def default_policy_path(inventory_path: str | Path) -> Path:
    return Path(inventory_path).with_name(CREDENTIAL_POLICY_NAME)

