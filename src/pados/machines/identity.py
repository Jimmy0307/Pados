# SPDX-License-Identifier: AGPL-3.0-only
"""Stable machine identity derived from the SSH host public key.

Hostname and IP address are location, never identity. ``machine_id`` is
``"mach-" + sha256("pados-machine-identity\\n" + host public key blob)[:20]``, so
the same host key at a new address is the same machine, and a different host
key at a known address is a different (possibly spoofed) machine that is never
silently re-enrolled. Only public host keys are handled here; a private key is
not a valid input anywhere in this module.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
import struct
from dataclasses import dataclass

from pados.machines.errors import MachineError

IDENTITY_DOMAIN = b"pados-machine-identity\n"
MACHINE_ID_PATTERN = r"^mach-[0-9a-f]{20}$"
FINGERPRINT_PATTERN = r"^SHA256:[A-Za-z0-9+/]{43}$"
_MACHINE_ID = re.compile(MACHINE_ID_PATTERN)
_FINGERPRINT = re.compile(FINGERPRINT_PATTERN)
# Host key algorithms PADOS pins; the number of SSH wire-format fields of each blob.
HOST_KEY_FIELDS: dict[str, int] = {
    "ssh-ed25519": 2,
    "ecdsa-sha2-nistp256": 3,
    "ecdsa-sha2-nistp384": 3,
    "ecdsa-sha2-nistp521": 3,
    "ssh-rsa": 3,
}
MAX_KEY_BLOB = 1600
_B64 = re.compile(r"^[A-Za-z0-9+/]{16,2200}={0,2}$")


def _wire_fields(blob: bytes) -> list[bytes]:
    fields: list[bytes] = []
    offset = 0
    while offset < len(blob):
        if offset + 4 > len(blob):
            raise MachineError("HOST_KEY_INVALID")
        (length,) = struct.unpack(">I", blob[offset:offset + 4])
        offset += 4
        if length > len(blob) - offset:
            raise MachineError("HOST_KEY_INVALID")
        fields.append(blob[offset:offset + length])
        offset += length
    return fields


def derive_machine_id(blob: bytes) -> str:
    return "mach-" + hashlib.sha256(IDENTITY_DOMAIN + blob).hexdigest()[:20]


def is_machine_id(value: object) -> bool:
    return isinstance(value, str) and _MACHINE_ID.fullmatch(value) is not None


def is_fingerprint(value: object) -> bool:
    return isinstance(value, str) and _FINGERPRINT.fullmatch(value) is not None


@dataclass(frozen=True)
class HostKey:
    """One SSH host public key (algorithm name + wire-format blob)."""

    key_type: str
    blob: bytes

    def __post_init__(self) -> None:
        expected = HOST_KEY_FIELDS.get(self.key_type)
        if expected is None:
            raise MachineError("HOST_KEY_TYPE_UNSUPPORTED")
        if not isinstance(self.blob, bytes) or not 0 < len(self.blob) <= MAX_KEY_BLOB:
            raise MachineError("HOST_KEY_INVALID")
        fields = _wire_fields(self.blob)
        if len(fields) != expected or fields[0] != self.key_type.encode("ascii"):
            raise MachineError("HOST_KEY_INVALID")
        if self.key_type == "ssh-ed25519" and len(fields[1]) != 32:
            raise MachineError("HOST_KEY_INVALID")

    @classmethod
    def from_openssh(cls, line: object) -> HostKey:
        """Parse ``"<type> <base64>"`` (an optional trailing comment is ignored)."""
        if not isinstance(line, str) or len(line) > 2400:
            raise MachineError("HOST_KEY_INVALID")
        parts = line.strip().split()
        if len(parts) < 2 or not _B64.fullmatch(parts[1]):
            raise MachineError("HOST_KEY_INVALID")
        try:
            blob = base64.b64decode(parts[1], validate=True)
        except (binascii.Error, ValueError):
            raise MachineError("HOST_KEY_INVALID") from None
        return cls(parts[0], blob)

    def openssh(self) -> str:
        return f"{self.key_type} {base64.b64encode(self.blob).decode('ascii')}"

    @property
    def fingerprint(self) -> str:
        """OpenSSH-style ``SHA256:<unpadded base64>`` fingerprint."""
        digest = base64.b64encode(hashlib.sha256(self.blob).digest()).decode("ascii").rstrip("=")
        return f"SHA256:{digest}"

    @property
    def machine_id(self) -> str:
        return derive_machine_id(self.blob)
