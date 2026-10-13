# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only Mission Control / doctor block for Herdr telemetry.

``herdr_visibility`` reads the configuration and the telemetry store only: it
never collects, executes Herdr, writes, creates the digest key or schedules
anything. Installation presence (fixture file present, or the ``herdr``
executable on PATH; never on Windows) is reported separately from runtime
availability, which is the latest persisted collection outcome (CR-06 M01). A
row is live only when its source time is proven fresh, the latest collection is
OK, its health is not UNREACHABLE and the source is installed; otherwise it is
shown stale with state UNKNOWN and a closed ``stale_reason``. Every value here is
telemetry (``semantic_authority: NONE``), never a PADOS run, job or acceptance state.
"""
from __future__ import annotations

from typing import Any

from pados.herdr import HERDR_LIVE_UAT
from pados.herdr.boundary import MEANING_SCOPE
from pados.herdr.config import HERDR_NOT_CONFIGURED, HerdrConfig, load_config
from pados.herdr.contract import SCHEMA_ID, SEMANTIC_AUTHORITY_NONE
from pados.herdr.diagnostics import COLLECTION_OK, HerdrCollectionOutcome
from pados.herdr.store import TERMINAL_HISTORY_DEFAULT

HERDR_DISABLED = "HERDR_DISABLED"
HERDR_UNAVAILABLE = "HERDR_UNAVAILABLE"
HERDR_DEGRADED = "HERDR_DEGRADED"
HERDR_NOT_OBSERVED = "HERDR_NOT_OBSERVED"
HERDR_OBSERVED = "HERDR_OBSERVED"


def _base() -> dict[str, Any]:
    return {"schema_id": SCHEMA_ID, "semantic_authority": SEMANTIC_AUTHORITY_NONE, "projection_only": True,
            "meaning_scope": MEANING_SCOPE, "terminal_content": TERMINAL_HISTORY_DEFAULT.value,
            "herdr_live_uat": HERDR_LIVE_UAT, "agents": []}


def _runtime(last: dict[str, Any] | None) -> str:
    if last is None:
        return "NOT_COLLECTED"
    outcome = HerdrCollectionOutcome(last["outcome"])
    if outcome in COLLECTION_OK:
        return "REACHABLE"
    return "UNREACHABLE" if outcome is HerdrCollectionOutcome.SOURCE_UNREACHABLE else "DOCUMENT_REFUSED"


def herdr_visibility(config_path: str) -> dict[str, Any]:
    if not config_path:
        return {**_base(), "status": HERDR_NOT_CONFIGURED}
    config = load_config(config_path)
    if not isinstance(config, HerdrConfig):
        return {**_base(), **config}
    view = {**_base(), "source": config.source_kind.value, "stale_after_seconds": config.stale_after_seconds}
    if not config.enabled:
        return {**view, "status": HERDR_DISABLED}
    installed = config.source().installed()
    store = config.store()
    last = store.latest_collection()
    agents = store.latest(stale_after_seconds=config.stale_after_seconds, not_installed=not installed)
    runtime = _runtime(last)
    if not installed or runtime == "UNREACHABLE":
        status = HERDR_UNAVAILABLE
    elif runtime == "DOCUMENT_REFUSED":
        status = HERDR_DEGRADED
    elif runtime == "NOT_COLLECTED":
        status = HERDR_NOT_OBSERVED
    else:
        status = HERDR_OBSERVED
    stale = sum(1 for agent in agents if agent["stale"])
    return {**view, "status": status, "source_installed": installed, "source_runtime": runtime,
            "latest_collection": last, "agents": agents, "agents_total": len(agents),
            "agents_live": len(agents) - stale, "agents_stale": stale}
