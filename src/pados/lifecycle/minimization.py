# SPDX-License-Identifier: AGPL-3.0-only
"""Data minimization guard for every new PADOS persistence point.

``assert_minimized`` is called by writers of operational records (notification
outbox, connectivity history, Herdr telemetry, machine profiles) before a record
reaches disk. It fails closed when a record carries a prohibited field name,
credential-shaped material, a non-identifier key or more data than the bounds
allow, so a plaintext password, API key, terminal scrollback, full context or
raw HTTP body cannot be persisted by accident.

The guard is a backstop, not a schema: each writer passes ``allowed_keys`` (its
explicit top-level record schema), so unexpected fields are refused rather than
judged by name. SecretRef strings (``secretref://...``) are references and are
allowed, including under credential-named fields such as ``password_ref``.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from typing import Any

from pados.execution.context import REDACTED_MARKER, redact
from pados.lifecycle.retention import ProhibitedPersistence

# Field names (normalized: lower-case, separators removed) that may never be persisted.
_PROHIBITED_FIELDS: dict[str, ProhibitedPersistence] = {
    "password": ProhibitedPersistence.PLAINTEXT_PASSWORD,
    "passwd": ProhibitedPersistence.PLAINTEXT_PASSWORD,
    "passphrase": ProhibitedPersistence.PLAINTEXT_PASSWORD,
    "plaintextpassword": ProhibitedPersistence.PLAINTEXT_PASSWORD,
    "apikey": ProhibitedPersistence.API_CREDENTIAL,
    "secret": ProhibitedPersistence.API_CREDENTIAL,
    "clientsecret": ProhibitedPersistence.API_CREDENTIAL,
    "token": ProhibitedPersistence.API_CREDENTIAL,
    "accesstoken": ProhibitedPersistence.API_CREDENTIAL,
    "privatekey": ProhibitedPersistence.API_CREDENTIAL,
    "context": ProhibitedPersistence.FULL_RECONSTRUCTED_CONTEXT,
    "fullcontext": ProhibitedPersistence.FULL_RECONSTRUCTED_CONTEXT,
    "reconstructedcontext": ProhibitedPersistence.FULL_RECONSTRUCTED_CONTEXT,
    "prompt": ProhibitedPersistence.DUPLICATED_PROVIDER_CONTEXT,
    "providercontext": ProhibitedPersistence.DUPLICATED_PROVIDER_CONTEXT,
    "messages": ProhibitedPersistence.DUPLICATED_PROVIDER_CONTEXT,
    "scrollback": ProhibitedPersistence.FULL_TERMINAL_HISTORY,
    "terminalhistory": ProhibitedPersistence.FULL_TERMINAL_HISTORY,
    "terminaloutput": ProhibitedPersistence.FULL_TERMINAL_HISTORY,
    "panecontent": ProhibitedPersistence.FULL_TERMINAL_HISTORY,
    "scratch": ProhibitedPersistence.RAW_SANDBOX_SCRATCH,
    "reasoning": ProhibitedPersistence.TEMPORARY_REASONING,
    "thoughts": ProhibitedPersistence.TEMPORARY_REASONING,
    "rawbody": ProhibitedPersistence.RAW_HTTP_BODY,
    "requestbody": ProhibitedPersistence.RAW_HTTP_BODY,
    "responsebody": ProhibitedPersistence.RAW_HTTP_BODY,
}
_CREDENTIAL_CATEGORIES = frozenset({ProhibitedPersistence.PLAINTEXT_PASSWORD, ProhibitedPersistence.API_CREDENTIAL})
# Keys are plain ASCII identifiers: no Unicode lookalikes, zero-width characters or
# punctuation that would let a prohibited name slip past normalization.
# Dots are excluded so a dotted path in allow_paths is unambiguous.
_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_SEPARATORS = re.compile(r"[_-]+")
_SECRET_REF = re.compile(r"^secretref://[a-z0-9_-]+/[A-Za-z0-9_./-]+$")
DEFAULT_MAX_STRING = 2048
DEFAULT_MAX_ITEMS = 256
DEFAULT_MAX_TOTAL_BYTES = 64 * 1024
_MAX_DEPTH = 8
_INT_BOUND = 2**63
# Filesystem identities: Windows reports a 64-bit volume serial as st_dev and up to 128-bit file ids as st_ino.
FILESYSTEM_ID_BOUND = 2**128


class DataMinimizationViolation(ValueError):
    def __init__(self, code: str, path: str, category: ProhibitedPersistence | None = None) -> None:
        super().__init__(f"{code}:{path}")
        self.code = code
        self.path = path
        self.category = category


def _normalize(name: str) -> str:
    return _SEPARATORS.sub("", name).lower()


def prohibited_field(name: str) -> ProhibitedPersistence | None:
    return _PROHIBITED_FIELDS.get(_normalize(name))


def is_secret_ref(value: object) -> bool:
    return isinstance(value, str) and _SECRET_REF.fullmatch(value) is not None


def assert_minimized(
    record: Mapping[str, Any],
    *,
    allowed_keys: Iterable[str] | None = None,
    allow_paths: Iterable[str] = (),
    max_string: int = DEFAULT_MAX_STRING,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    filesystem_id_paths: Iterable[str] = (),
) -> None:
    """Raise ``DataMinimizationViolation`` unless ``record`` is safe to persist.

    ``allowed_keys`` is the writer's top-level schema; any other key is refused.
    ``allow_paths`` lists exact dotted paths a writer reviewed (for example a
    bounded ``context_summary``-style field literally named ``context``); the
    value at such a path must be a single bounded string, never a structure.
    ``filesystem_id_paths`` lists exact dotted paths holding an ``st_dev``/``st_ino``
    value; there an integer must be in ``[0, FILESYSTEM_ID_BOUND)`` instead of the
    generic signed 64-bit bound. Every other integer keeps the generic bound.
    """
    if not isinstance(record, Mapping):
        raise DataMinimizationViolation("RECORD_MUST_BE_MAPPING", "")
    schema = frozenset(allowed_keys) if allowed_keys is not None else None
    reviewed = frozenset(allow_paths)
    filesystem_ids = frozenset(filesystem_id_paths)
    total = 0

    def account(size: int, path: str) -> None:
        nonlocal total
        total += size
        if total > max_total_bytes:
            raise DataMinimizationViolation("RECORD_TOO_LARGE", path)

    def check_key(key: object, path: str) -> str:
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise DataMinimizationViolation("FIELD_NAME_INVALID", path)
        if redact(key) == REDACTED_MARKER:
            raise DataMinimizationViolation("CREDENTIAL_SHAPED_FIELD_NAME", path, ProhibitedPersistence.API_CREDENTIAL)
        account(len(key), path)
        return key

    def walk(value: Any, path: str, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise DataMinimizationViolation("RECORD_TOO_DEEP", path)
        if isinstance(value, Mapping):
            if len(value) > max_items:
                raise DataMinimizationViolation("COLLECTION_TOO_LARGE", path)
            for key, item in value.items():
                name = check_key(key, path)
                child = f"{path}.{name}" if path else name
                if depth == 0 and schema is not None and name not in schema:
                    raise DataMinimizationViolation("FIELD_NOT_IN_SCHEMA", child)
                if child in reviewed:
                    if not isinstance(item, str):
                        raise DataMinimizationViolation("REVIEWED_FIELD_MUST_BE_STRING", child)
                    walk(item, child, depth + 1)
                    continue
                category = prohibited_field(name)
                if category is not None and not (category in _CREDENTIAL_CATEGORIES and is_secret_ref(item)):
                    raise DataMinimizationViolation("PROHIBITED_FIELD", child, category)
                walk(item, child, depth + 1)
        elif isinstance(value, (list, tuple)):
            if len(value) > max_items:
                raise DataMinimizationViolation("COLLECTION_TOO_LARGE", path)
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]", depth + 1)
        elif isinstance(value, str):
            account(len(value.encode("utf-8")), path)
            if is_secret_ref(value):
                return
            if len(value) > max_string:
                raise DataMinimizationViolation("STRING_TOO_LONG", path)
            if redact(value) == REDACTED_MARKER:
                raise DataMinimizationViolation("CREDENTIAL_SHAPED_VALUE", path, ProhibitedPersistence.API_CREDENTIAL)
        elif value is None or isinstance(value, bool):
            account(8, path)
        elif isinstance(value, int):
            if path in filesystem_ids:
                if not 0 <= value < FILESYSTEM_ID_BOUND:
                    raise DataMinimizationViolation("NUMBER_OUT_OF_RANGE", path)
            elif not -_INT_BOUND <= value <= _INT_BOUND:
                raise DataMinimizationViolation("NUMBER_OUT_OF_RANGE", path)
            account(8, path)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise DataMinimizationViolation("NUMBER_OUT_OF_RANGE", path)
            account(8, path)
        else:
            raise DataMinimizationViolation("UNSUPPORTED_VALUE_TYPE", path)

    walk(record, "", 0)


def assert_no_credentials(document: Any, *, max_depth: int = 32) -> None:
    """Credential-only check for large evidence documents (SBOM, provenance, manifests).

    These are release evidence, not operational records: their shape is fixed by
    external standards (``$schema``, ``bom-ref``) and they legitimately hold
    hundreds of entries, so ``assert_minimized`` bounds do not apply. Every key
    and string value must still be free of credential-shaped material.
    """

    def walk(value: Any, path: str, depth: int) -> None:
        if depth > max_depth:
            raise DataMinimizationViolation("RECORD_TOO_DEEP", path)
        if isinstance(value, Mapping):
            for key, item in value.items():
                name = str(key)
                if redact(name) == REDACTED_MARKER:
                    raise DataMinimizationViolation("CREDENTIAL_SHAPED_FIELD_NAME", path,
                                                    ProhibitedPersistence.API_CREDENTIAL)
                walk(item, f"{path}.{name}" if path else name, depth + 1)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]", depth + 1)
        elif isinstance(value, str) and not is_secret_ref(value) and redact(value) == REDACTED_MARKER:
            raise DataMinimizationViolation("CREDENTIAL_SHAPED_VALUE", path, ProhibitedPersistence.API_CREDENTIAL)

    walk(document, "", 0)
