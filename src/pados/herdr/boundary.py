# SPDX-License-Identifier: AGPL-3.0-only
"""Authority boundary between Herdr telemetry and PADOS authority.

Every Herdr state maps to a TELEMETRY-ONLY observation. None of these meanings is
a PADOS run, job, validation or acceptance state, and this module deliberately
offers no function that turns a Herdr state into acceptance, success or failure:
HERDR WORKING != PADOS ACCEPTED, HERDR IDLE != PADOS SUCCESS, HERDR BLOCKED !=
PADOS FAILED. ``assert_no_authority`` is the fail-closed guard the adapter and the
store apply to every record before it leaves the adapter or reaches disk.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from pados.herdr.contract import (
    RECORD_KEYS,
    SCHEMA_ID,
    SEMANTIC_AUTHORITY_NONE,
    HerdrAgentKind,
    HerdrAgentState,
    HerdrHealth,
)

MEANING_SCOPE = "TELEMETRY_ONLY"


class TelemetryMeaning(StrEnum):
    AGENT_ACTIVITY_OBSERVED = "AGENT_ACTIVITY_OBSERVED"
    AGENT_IDLE_OBSERVED = "AGENT_IDLE_OBSERVED"
    AGENT_BLOCKED_OBSERVED = "AGENT_BLOCKED_OBSERVED"
    AGENT_INPUT_REQUEST_OBSERVED = "AGENT_INPUT_REQUEST_OBSERVED"
    AGENT_EXIT_OBSERVED = "AGENT_EXIT_OBSERVED"
    AGENT_STATE_NOT_OBSERVED = "AGENT_STATE_NOT_OBSERVED"


STATE_MEANING: Mapping[HerdrAgentState, TelemetryMeaning] = MappingProxyType({
    HerdrAgentState.WORKING: TelemetryMeaning.AGENT_ACTIVITY_OBSERVED,
    HerdrAgentState.IDLE: TelemetryMeaning.AGENT_IDLE_OBSERVED,
    HerdrAgentState.BLOCKED: TelemetryMeaning.AGENT_BLOCKED_OBSERVED,
    HerdrAgentState.WAITING_INPUT: TelemetryMeaning.AGENT_INPUT_REQUEST_OBSERVED,
    HerdrAgentState.EXITED: TelemetryMeaning.AGENT_EXIT_OBSERVED,
    HerdrAgentState.UNKNOWN: TelemetryMeaning.AGENT_STATE_NOT_OBSERVED,
})

# Raw-document keys (normalized: lower-case, separators removed) through which a
# Herdr document might claim PADOS authority. They are ignored and counted.
AUTHORITY_CLAIM_KEYS: frozenset[str] = frozenset({
    "padosstatus", "padosstate", "padosresult", "padosrun", "acceptance", "accepted", "accept",
    "approval", "approvals", "approved", "approve", "reject", "rejected", "rejection", "verdict",
    "result", "resultreceipt", "executionreceipt", "receipt", "receipts", "semanticstate",
    "semanticauthority", "authority", "scopeversion", "cas", "commit", "committed", "audit",
    "auditchain", "memory", "memoryrecord", "decision", "runstatus", "runstate", "jobstatus",
    "jobcontrol", "cancel", "canceljob", "start", "startjob", "schedule", "dispatch", "route",
    "provider", "validation", "validated", "success", "succeeded", "failure", "failed",
    # Project / work-package association is an operator binding in PADOS config, never a source claim.
    "projectref", "projectid", "workpackageref", "workpackageid",
})
# Raw-document keys through which a Herdr document might claim a PADOS identity.
# The machine reference is bound by PADOS configuration, never taken from Herdr.
IDENTITY_CLAIM_KEYS: frozenset[str] = frozenset({
    "machineid", "machineref", "machine", "hostname", "host", "nodeid", "ip", "ipaddress", "address",
})

# PADOS authority vocabulary that must never appear as a telemetry value.
AUTHORITY_TERMS: frozenset[str] = frozenset({
    "ACCEPTED", "APPROVED", "REJECTED", "COMMITTED", "VALIDATED", "SUCCEEDED", "SUCCESS", "FAILED",
    "FAILURE", "PASSED", "COMPLETED", "VERIFIED", "PROMOTED", "AUTHORIZED",
})
_SEPARATORS = re.compile(r"[\s_.:-]+")


class HerdrAuthorityViolation(ValueError):
    def __init__(self, code: str, path: str = "") -> None:
        super().__init__(f"{code}:{path}" if path else code)
        self.code = code
        self.path = path


def normalized_key(name: str) -> str:
    return _SEPARATORS.sub("", name).lower()


def is_authority_claim_key(name: str) -> bool:
    return normalized_key(name) in AUTHORITY_CLAIM_KEYS


def is_authority_term(value: str) -> bool:
    return _SEPARATORS.sub("", value).upper() in AUTHORITY_TERMS


def telemetry_meaning(state: HerdrAgentState) -> TelemetryMeaning:
    """The telemetry observation a Herdr state stands for (never a PADOS state)."""
    return STATE_MEANING[state]


def assert_no_authority(record: Mapping[str, Any]) -> None:
    """Raise ``HerdrAuthorityViolation`` unless ``record`` is pure telemetry.

    The record must use exactly the telemetry schema, declare
    ``semantic_authority == NONE``, carry a Herdr state and agent kind from the
    closed vocabularies (never a PADOS state), hold only scalar values and no
    PADOS authority term as any value. Source-controlled free text cannot reach
    a record at all (keyed-digest refs, closed agent kind, operator bindings), so
    this guard is a backstop, not the boundary itself.
    """
    if not isinstance(record, Mapping):
        raise HerdrAuthorityViolation("HERDR_RECORD_NOT_MAPPING")
    keys = set(record)
    if keys != set(RECORD_KEYS):
        extra = sorted(str(key) for key in keys - set(RECORD_KEYS))
        raise HerdrAuthorityViolation("HERDR_RECORD_SCHEMA_MISMATCH", ",".join(extra[:4]))
    if record["schema_id"] != SCHEMA_ID:
        raise HerdrAuthorityViolation("HERDR_SCHEMA_ID_INVALID", "schema_id")
    if record["semantic_authority"] != SEMANTIC_AUTHORITY_NONE:
        raise HerdrAuthorityViolation("HERDR_CLAIMS_SEMANTIC_AUTHORITY", "semantic_authority")
    if record["state"] not in {state.value for state in HerdrAgentState}:
        raise HerdrAuthorityViolation("HERDR_STATE_NOT_TELEMETRY", "state")
    if record["health"] not in {health.value for health in HerdrHealth}:
        raise HerdrAuthorityViolation("HERDR_HEALTH_INVALID", "health")
    if record["agent_kind"] not in {kind.value for kind in HerdrAgentKind}:
        raise HerdrAuthorityViolation("HERDR_AGENT_KIND_INVALID", "agent_kind")
    for path, value in record.items():
        if not isinstance(value, (str, type(None))):
            raise HerdrAuthorityViolation("HERDR_VALUE_NOT_SCALAR", path)
        if isinstance(value, str) and is_authority_term(value):
            raise HerdrAuthorityViolation("HERDR_VALUE_IS_AUTHORITY_TERM", path)
