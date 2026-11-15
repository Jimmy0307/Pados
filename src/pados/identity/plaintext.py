# SPDX-License-Identifier: AGPL-3.0-only
"""Refuse identity configuration files that carry a plaintext credential, in the style of ``pados.remote.auth``.

A field whose name is a credential (``client_secret``, ``password``, ``token``, ``private_key`` ...) or ends like one,
at any depth, makes the whole file invalid. A credential is only ever referenced, by the *name* of an environment
variable (``client_secret_env``). Mappings whose keys are data (project ids) have only their values inspected.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pados.lifecycle.minimization import prohibited_field
from pados.lifecycle.retention import ProhibitedPersistence

_CREDENTIAL_CATEGORIES = frozenset({ProhibitedPersistence.PLAINTEXT_PASSWORD, ProhibitedPersistence.API_CREDENTIAL})
# Qualified forms (``client_secret``, ``refresh_token``) are plaintext too; ``*_env`` references are not.
_PLAINTEXT_SUFFIXES = ("password", "passwd", "passphrase", "secret", "token", "apikey", "privatekey", "credential",
                       "assertion", "proof")


def is_plaintext_field(key: object) -> bool:
    normalized = re.sub(r"[\s_.-]+", "", str(key)).lower()
    return prohibited_field(str(key)) in _CREDENTIAL_CATEGORIES or normalized.endswith(_PLAINTEXT_SUFFIXES)


def contains_plaintext_credential(value: Any, *, data_keyed: frozenset[str] = frozenset(),
                                  keys_are_data: bool = False) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not keys_are_data and is_plaintext_field(key):
                return True
            if contains_plaintext_credential(item, data_keyed=data_keyed, keys_are_data=str(key) in data_keyed):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(contains_plaintext_credential(item, data_keyed=data_keyed) for item in value)
    return False
