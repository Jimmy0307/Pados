# SPDX-License-Identifier: AGPL-3.0-only
"""Simple Machine Connection V1: one operator-owned inventory of physical machines.

Authority boundary: this package is operational configuration and observation
only. It never writes Memory, semantic state, receipts or CAS, never imports
Herdr (a profile carries only optional Herdr metadata), exposes no API route and
no shell. Identity is the pinned SSH host key, never a hostname or IP.
Credentials are references only (``secretref://``, ``ssh-agent://``,
``oscred://``); a bootstrap password may authenticate once and is never stored.
Credential use needs a separate operator credential policy; the ordinary
connection test authenticates only (``ssh -N``, no remote command). Privileged
operations (terminal, shell, admin, file transfer) need a short-lived grant
backed by two operators' HMAC proofs, bound to the full request and executed
once; a remote session can never satisfy it.
"""
from __future__ import annotations

from pados.machines.errors import MachineError
from pados.machines.identity import HostKey, derive_machine_id
from pados.machines.inventory import InventoryStore, MachineInventory, check_compute_nodes
from pados.machines.operators import OperatorAuthenticator, OperatorProof
from pados.machines.policy import CredentialPolicy
from pados.machines.profile import (
    AuthMethod,
    ConnectionState,
    HerdrMetadata,
    MachineProfile,
    MachineRole,
    Platform,
)
from pados.machines.visibility import machine_visibility

__all__ = [
    "AuthMethod",
    "ConnectionState",
    "CredentialPolicy",
    "HerdrMetadata",
    "HostKey",
    "InventoryStore",
    "MachineError",
    "MachineInventory",
    "MachineProfile",
    "MachineRole",
    "OperatorAuthenticator",
    "OperatorProof",
    "Platform",
    "check_compute_nodes",
    "derive_machine_id",
    "machine_visibility",
]
