# SPDX-License-Identifier: AGPL-3.0-only
"""Optional Herdr configuration (``PADOS_HERDR_CONFIG``, wired by the integrator).

Unset means HERDR_NOT_CONFIGURED and PADOS is fully functional without Herdr.
A config that cannot be read or parsed, or that has an unknown key or an
invalid value, is HERDR_CONFIG_INVALID as a whole: nothing is collected. Config
JSON (operator file only; never accepted from an API or from Herdr)::

    {"enabled": true,
     "source": "fixture" | "command",
     "fixture_path": "<file>",              (required for "fixture")
     "store_path": "<runtime dir>/herdr/telemetry.json",
     "machine_ref": "mach-<20 lowercase hex>",
     "stale_after_seconds": 120,
     "max_records": 2000,
     "max_bytes": 1048576,
     "command_timeout_seconds": 5,
     "bindings": {"<source tag>": {"project_ref": "<PADOS project id>",
                                   "work_package_ref": "<PADOS work package id>"}}}

``machine_ref`` is the PADOS machine_id the Herdr source runs on; Herdr cannot
choose or change it. ``bindings`` is the only way a pane gets a project /
work-package reference: a pane whose source tag is not bound gets none, and the
tag itself is never stored (CR-06 H01).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pados.herdr.adapter import (
    COMMAND_TIMEOUT_DEFAULT,
    MAX_TAG_CHARS,
    STALE_AFTER_DEFAULT,
    CommandHerdrSource,
    FixtureHerdrSource,
    HerdrAdapter,
    HerdrBinding,
    HerdrSource,
    HerdrSourceConfigError,
)
from pados.herdr.contract import MACHINE_REF_PATTERN
from pados.herdr.diagnostics import HerdrCollectionOutcome
from pados.herdr.store import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_RECORDS,
    HerdrStoreError,
    HerdrTelemetryStore,
    validate_stale_after,
)

HERDR_NOT_CONFIGURED = "HERDR_NOT_CONFIGURED"
HERDR_CONFIG_INVALID = "HERDR_CONFIG_INVALID"
MAX_CONFIG_BYTES = 64 * 1024
CONFIG_KEYS = frozenset({"enabled", "source", "fixture_path", "store_path", "machine_ref", "stale_after_seconds",
                         "max_records", "max_bytes", "command_timeout_seconds", "bindings"})
MAX_BINDINGS = 256
BINDING_KEYS = frozenset({"project_ref", "work_package_ref"})
_MACHINE_REF = re.compile(MACHINE_REF_PATTERN)


class HerdrSourceKind(StrEnum):
    FIXTURE = "fixture"
    COMMAND = "command"


class HerdrConfigError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"{HERDR_CONFIG_INVALID}:{reason}")
        self.code = HERDR_CONFIG_INVALID
        self.reason = reason


def _path(data: dict[str, Any], key: str) -> Path:
    value = data.get(key)
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value:
        raise HerdrConfigError(f"{key.upper()}_INVALID")
    return Path(value)


def _int(data: dict[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HerdrConfigError(f"{key.upper()}_INVALID")
    return int(value)


def _bindings(data: dict[str, Any]) -> tuple[tuple[str, HerdrBinding], ...]:
    raw = data.get("bindings", {})
    if not isinstance(raw, dict) or len(raw) > MAX_BINDINGS:
        raise HerdrConfigError("BINDINGS_INVALID")
    bindings = []
    for tag, value in raw.items():
        if not isinstance(tag, str) or not 1 <= len(tag) <= MAX_TAG_CHARS or not isinstance(value, dict) \
                or "project_ref" not in value or set(value) - BINDING_KEYS:
            raise HerdrConfigError("BINDINGS_INVALID")
        try:
            bindings.append((tag, HerdrBinding(value["project_ref"], value.get("work_package_ref"))))
        except HerdrSourceConfigError as exc:
            raise HerdrConfigError(exc.code) from exc
    return tuple(sorted(bindings, key=lambda item: item[0]))


@dataclass(frozen=True)
class HerdrConfig:
    enabled: bool
    source_kind: HerdrSourceKind
    store_path: Path
    machine_ref: str
    fixture_path: Path | None = None
    stale_after_seconds: int = STALE_AFTER_DEFAULT
    max_records: int = DEFAULT_MAX_RECORDS
    max_bytes: int = DEFAULT_MAX_BYTES
    command_timeout_seconds: float = COMMAND_TIMEOUT_DEFAULT
    bindings: tuple[tuple[str, HerdrBinding], ...] = ()

    @property
    def store_root(self) -> Path:
        """The directory Lifecycle storage accounting measures as HERDR_TELEMETRY."""
        return self.store_path.parent

    @classmethod
    def load(cls, path: str | Path) -> HerdrConfig:
        try:
            with Path(path).open("rb") as handle:
                raw = handle.read(MAX_CONFIG_BYTES + 1)
        except OSError as exc:
            raise HerdrConfigError("CONFIG_UNREADABLE") from exc
        if len(raw) > MAX_CONFIG_BYTES:
            raise HerdrConfigError("CONFIG_TOO_LARGE")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError) as exc:
            raise HerdrConfigError("CONFIG_NOT_JSON") from exc
        if not isinstance(data, dict):
            raise HerdrConfigError("CONFIG_NOT_AN_OBJECT")
        return cls.parse(data)

    @classmethod
    def parse(cls, data: dict[str, Any]) -> HerdrConfig:
        unknown = set(data) - CONFIG_KEYS
        if unknown:
            raise HerdrConfigError("CONFIG_UNKNOWN_KEY")
        enabled = data.get("enabled", False)
        if not isinstance(enabled, bool):
            raise HerdrConfigError("ENABLED_INVALID")
        try:
            source_kind = HerdrSourceKind(data.get("source", HerdrSourceKind.FIXTURE.value))
        except ValueError as exc:
            raise HerdrConfigError("SOURCE_INVALID") from exc
        machine_ref = data.get("machine_ref")
        if not isinstance(machine_ref, str) or not _MACHINE_REF.fullmatch(machine_ref):
            raise HerdrConfigError("MACHINE_REF_INVALID")
        store_path = _path(data, "store_path")
        fixture_path = None
        if source_kind is HerdrSourceKind.FIXTURE or data.get("fixture_path") not in (None, ""):
            fixture_path = _path(data, "fixture_path")
        timeout = data.get("command_timeout_seconds", COMMAND_TIMEOUT_DEFAULT)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise HerdrConfigError("COMMAND_TIMEOUT_SECONDS_INVALID")
        config = cls(enabled=enabled, source_kind=source_kind, store_path=store_path, machine_ref=machine_ref,
                     fixture_path=fixture_path,
                     stale_after_seconds=_int(data, "stale_after_seconds", STALE_AFTER_DEFAULT),
                     max_records=_int(data, "max_records", DEFAULT_MAX_RECORDS),
                     max_bytes=_int(data, "max_bytes", DEFAULT_MAX_BYTES),
                     command_timeout_seconds=float(timeout), bindings=_bindings(data))
        try:  # every bound is checked once, by the component that enforces it
            validate_stale_after(config.stale_after_seconds)
            config.store()
            config.source()
        except HerdrStoreError as exc:
            raise HerdrConfigError(exc.code) from exc
        except HerdrSourceConfigError as exc:
            raise HerdrConfigError(exc.code) from exc
        return config

    def source(self) -> HerdrSource:
        if self.source_kind is HerdrSourceKind.FIXTURE:
            if self.fixture_path is None:
                raise HerdrSourceConfigError("HERDR_FIXTURE_PATH_REQUIRED")
            return FixtureHerdrSource(self.fixture_path)
        return CommandHerdrSource(timeout_seconds=self.command_timeout_seconds)

    def adapter(self) -> HerdrAdapter:
        """The adapter for this config (creates the store's digest key on first use)."""
        return HerdrAdapter(self.source(), machine_ref=self.machine_ref, ref_key=self.store().ref_key(),
                            bindings=dict(self.bindings), stale_after_seconds=self.stale_after_seconds)

    def collect(self) -> HerdrCollectionOutcome:
        """One collection into the store. Never scheduled here: a caller decides when."""
        return self.store().record_collection(self.adapter().collect())

    def store(self) -> HerdrTelemetryStore:
        return HerdrTelemetryStore(self.store_path, max_records=self.max_records, max_bytes=self.max_bytes)


def load_config(config_path: str) -> HerdrConfig | dict[str, str]:
    """The parsed config, or the NOT_CONFIGURED / CONFIG_INVALID projection (fail closed)."""
    if not config_path:
        return {"status": HERDR_NOT_CONFIGURED}
    try:
        return HerdrConfig.load(config_path)
    except HerdrConfigError as exc:
        return {"status": HERDR_CONFIG_INVALID, "reason": exc.reason}
