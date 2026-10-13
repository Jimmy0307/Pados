# SPDX-License-Identifier: AGPL-3.0-only
"""Herdr integration: agent runtime telemetry and session substrate (V1, contract level).

Herdr is a TELEMETRY source, never an authority. PADOS remains the governed
authority and orchestration layer. HERDR WORKING != PADOS ACCEPTED; HERDR IDLE !=
PADOS SUCCESS; HERDR BLOCKED != PADOS FAILED; HERDR SESSION != PADOS MEMORY;
HERDR TERMINAL HISTORY != PROJECT CONTEXT.

Nothing in this package schedules, routes, commits, approves, audits or writes
Memory. It translates bounded status documents into R1 telemetry records
(``DataKind.HERDR_TELEMETRY``) and projects them read-only. The live Herdr wire
format is unverified: HERDR_LIVE_UAT = PENDING.
"""
from __future__ import annotations

HERDR_LIVE_UAT = "PENDING"
