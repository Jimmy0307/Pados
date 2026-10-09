# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS-side Herdr telemetry contract ``pados.herdr.telemetry.v1``.

A record is one telemetry observation of one agent pane. No source-controlled
free text is ever part of it (CR-06 H01/M05):

* ``session_ref`` / ``pane_ref`` are PADOS keyed digests (``hs-``/``hp-`` + 16 hex
  of HMAC-SHA256 under a per-store random key), never the raw Herdr ids;
* ``agent_kind`` is a closed vocabulary mapped from known source values only;
  there is no label field;
* ``project_ref`` / ``work_package_ref`` come only from the operator's binding
  map in PADOS configuration;
* ``machine_ref`` is the configured PADOS ``machine_id``, never a hostname.

Time is split (CR-06 H02): ``collected_at`` is the PADOS collection clock,
``source_observed_at`` the validated document-level snapshot time (``None`` when
the source did not prove freshness; such a record is never projected live).
``semantic_authority`` is always ``NONE``. The live Herdr format is unverified.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from pados.lifecycle.minimization import assert_minimized

SCHEMA_ID = "pados.herdr.telemetry.v1"
SEMANTIC_AUTHORITY_NONE = "NONE"

MACHINE_REF_PATTERN = r"^mach-[0-9a-f]{20}$"
SESSION_REF_PATTERN = r"^hs-[0-9a-f]{16}$"
PANE_REF_PATTERN = r"^hp-[0-9a-f]{16}$"
# PADOS project / work-package identifiers, written by the operator in config bindings only.
PADOS_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$"
MAX_REF_CHARS = 80
# Tolerated forward skew between the source clock and the PADOS collector.
CLOCK_SKEW = timedelta(seconds=60)
RECORD_MAX_BYTES = 4096

RECORD_KEYS: tuple[str, ...] = (
    "schema_id", "machine_ref", "session_ref", "pane_ref", "agent_kind", "state", "collected_at",
    "source_observed_at", "last_transition_at", "health", "project_ref", "work_package_ref",
    "semantic_authority",
)


class HerdrAgentState(StrEnum):
    WORKING = "WORKING"
    IDLE = "IDLE"
    BLOCKED = "BLOCKED"
    WAITING_INPUT = "WAITING_INPUT"
    EXITED = "EXITED"
    UNKNOWN = "UNKNOWN"


class HerdrHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNREACHABLE = "UNREACHABLE"


class HerdrAgentKind(StrEnum):
    CLAUDE_CODE = "CLAUDE_CODE"
    CODEX = "CODEX"
    GEMINI = "GEMINI"
    SHELL = "SHELL"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


# UNVERIFIED known source values (normalized: lower-case, separators removed).
# Any other present value is OTHER and a missing value is UNKNOWN; the source value is never copied.
AGENT_KIND_MAP: Mapping[str, HerdrAgentKind] = {
    "claude": HerdrAgentKind.CLAUDE_CODE, "claudecode": HerdrAgentKind.CLAUDE_CODE,
    "codex": HerdrAgentKind.CODEX, "codexcli": HerdrAgentKind.CODEX,
    "gemini": HerdrAgentKind.GEMINI, "geminicli": HerdrAgentKind.GEMINI,
    "shell": HerdrAgentKind.SHELL, "bash": HerdrAgentKind.SHELL, "zsh": HerdrAgentKind.SHELL,
    "sh": HerdrAgentKind.SHELL, "fish": HerdrAgentKind.SHELL, "pwsh": HerdrAgentKind.SHELL,
    "powershell": HerdrAgentKind.SHELL,
}


class HerdrTelemetryRecord(BaseModel):
    """One telemetry observation; an observation, never a PADOS state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_id: Literal["pados.herdr.telemetry.v1"] = "pados.herdr.telemetry.v1"
    machine_ref: str = Field(pattern=MACHINE_REF_PATTERN, max_length=25)
    session_ref: str = Field(pattern=SESSION_REF_PATTERN, max_length=19)
    pane_ref: str = Field(pattern=PANE_REF_PATTERN, max_length=19)
    agent_kind: HerdrAgentKind = HerdrAgentKind.UNKNOWN
    state: HerdrAgentState = HerdrAgentState.UNKNOWN
    collected_at: AwareDatetime
    source_observed_at: AwareDatetime | None = None
    last_transition_at: AwareDatetime | None = None
    health: HerdrHealth = HerdrHealth.HEALTHY
    project_ref: str | None = Field(default=None, pattern=PADOS_REF_PATTERN, max_length=MAX_REF_CHARS)
    work_package_ref: str | None = Field(default=None, pattern=PADOS_REF_PATTERN, max_length=MAX_REF_CHARS)
    semantic_authority: Literal["NONE"] = "NONE"

    @model_validator(mode="after")
    def _times_not_after_collection(self) -> HerdrTelemetryRecord:
        limit = self.collected_at + CLOCK_SKEW
        if self.source_observed_at is not None and self.source_observed_at > limit:
            raise ValueError("HERDR_SOURCE_TIME_AFTER_COLLECTION")
        if self.last_transition_at is not None and self.last_transition_at > limit:
            raise ValueError("HERDR_TRANSITION_AFTER_COLLECTION")
        return self

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.machine_ref, self.session_ref, self.pane_ref)

    def to_row(self) -> dict[str, Any]:
        """The persisted JSON form (exactly ``RECORD_KEYS``)."""
        return self.model_dump(mode="json")


def assert_record_minimized(row: Mapping[str, Any]) -> None:
    """The shared minimization guard with the explicit telemetry schema."""
    assert_minimized(row, allowed_keys=RECORD_KEYS, max_string=128, max_items=len(RECORD_KEYS),
                     max_total_bytes=RECORD_MAX_BYTES)


def parse_aware(value: object) -> datetime | None:
    """A timezone-aware timestamp, or ``None`` when the value cannot be trusted."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None and stamp.utcoffset() is not None else None
