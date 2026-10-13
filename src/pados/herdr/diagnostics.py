# SPDX-License-Identifier: AGPL-3.0-only
"""Closed, content-free Herdr diagnostic and collection-outcome vocabularies (CR-06 M01/M03).

Diagnostics never carry exception text or source-controlled strings: a source
error that is not one of these codes becomes ``SOURCE_ERROR``. Collection
outcomes describe the telemetry pipe only; they are never PADOS run states.
"""
from __future__ import annotations

from enum import StrEnum


class HerdrDiagnostic(StrEnum):
    # document refusals (whole document)
    RAW_DOC_NOT_BYTES = "HERDR_RAW_DOC_NOT_BYTES"
    RAW_DOC_TOO_LARGE = "HERDR_RAW_DOC_TOO_LARGE"
    RAW_DOC_INVALID_JSON = "HERDR_RAW_DOC_INVALID_JSON"
    RAW_DOC_SHAPE_INVALID = "HERDR_RAW_DOC_SHAPE_INVALID"
    RAW_DOC_TOO_DEEP = "HERDR_RAW_DOC_TOO_DEEP"
    RAW_DOC_TOO_MANY_SESSIONS = "HERDR_RAW_DOC_TOO_MANY_SESSIONS"
    RAW_DOC_TOO_MANY_PANES = "HERDR_RAW_DOC_TOO_MANY_PANES"
    # document-level source timestamp (records kept, projected UNKNOWN)
    SOURCE_TIME_MISSING = "HERDR_SOURCE_TIME_MISSING"
    SOURCE_TIME_INVALID = "HERDR_SOURCE_TIME_INVALID"
    SOURCE_TIME_IN_FUTURE = "HERDR_SOURCE_TIME_IN_FUTURE"
    SOURCE_TIME_TOO_OLD = "HERDR_SOURCE_TIME_TOO_OLD"
    # source failures
    NOT_INSTALLED = "HERDR_NOT_INSTALLED"
    SOURCE_TIMEOUT = "HERDR_SOURCE_TIMEOUT"
    SOURCE_EXIT_NONZERO = "HERDR_SOURCE_EXIT_NONZERO"
    SOURCE_UNAVAILABLE = "HERDR_SOURCE_UNAVAILABLE"
    FIXTURE_UNAVAILABLE = "HERDR_FIXTURE_UNAVAILABLE"
    FIXTURE_UNREADABLE = "HERDR_FIXTURE_UNREADABLE"
    COMMAND_SOURCE_UNSUPPORTED_ON_WINDOWS = "HERDR_COMMAND_SOURCE_UNSUPPORTED_ON_WINDOWS"
    SOURCE_ERROR = "HERDR_SOURCE_ERROR"
    # per-entry refusals
    REF_INVALID = "HERDR_REF_INVALID"
    RECORD_INVALID = "HERDR_RECORD_INVALID"
    RECORD_NOT_MINIMIZED = "HERDR_RECORD_NOT_MINIMIZED"
    RECORD_CLAIMS_AUTHORITY = "HERDR_RECORD_CLAIMS_AUTHORITY"
    PANE_DUPLICATE = "HERDR_PANE_DUPLICATE"
    PANE_SHAPE_INVALID = "HERDR_PANE_SHAPE_INVALID"
    SESSION_SHAPE_INVALID = "HERDR_SESSION_SHAPE_INVALID"


def diagnostic(value: object) -> HerdrDiagnostic:
    """``value`` if it is a known diagnostic, otherwise ``SOURCE_ERROR`` (never echoed)."""
    if isinstance(value, HerdrDiagnostic):
        return value
    if isinstance(value, str):
        for member in HerdrDiagnostic:
            if member.value == value:
                return member
    return HerdrDiagnostic.SOURCE_ERROR


class HerdrCollectionOutcome(StrEnum):
    COLLECTED = "COLLECTED"
    COLLECTED_PARTIAL = "COLLECTED_PARTIAL"  # some entries refused
    UNCHANGED = "UNCHANGED"  # same document as the last collection: freshness is not refreshed
    DOCUMENT_REFUSED = "DOCUMENT_REFUSED"
    SOURCE_UNREACHABLE = "SOURCE_UNREACHABLE"


COLLECTION_OK: frozenset[HerdrCollectionOutcome] = frozenset({
    HerdrCollectionOutcome.COLLECTED, HerdrCollectionOutcome.COLLECTED_PARTIAL, HerdrCollectionOutcome.UNCHANGED,
})


class HerdrStaleReason(StrEnum):
    """Why a projected row is not live (closed vocabulary)."""

    SOURCE_TIME_UNPROVEN = "SOURCE_TIME_UNPROVEN"
    SOURCE_TIME_EXPIRED = "SOURCE_TIME_EXPIRED"
    LATEST_COLLECTION_UNHEALTHY = "LATEST_COLLECTION_UNHEALTHY"
    NOT_COLLECTED = "NOT_COLLECTED"
    SOURCE_HEALTH_UNREACHABLE = "SOURCE_HEALTH_UNREACHABLE"
    SOURCE_NOT_INSTALLED = "SOURCE_NOT_INSTALLED"
    NOT_IN_LATEST_SNAPSHOT = "NOT_IN_LATEST_SNAPSHOT"
