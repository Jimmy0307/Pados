# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic AI-Scientist-v3-shaped runs for the research pack (no upstream output, credentials or real paper).

The layout mirrors the upstream run directory (``results/<task>/<agent>/<run_id>/<phase>/...``) with invented,
public-safe content. ``ScriptedRuntime`` stands in for the external runtime: it is untrusted and returns whatever
the test scripts, including malformed or hostile output.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pados.packs.research.contracts import (
    CampaignRequest,
    ResearchRunOutput,
    ResearchRuntimeProfile,
    ResearchRuntimeTimeout,
    ResearchRuntimeUnavailable,
)


def profile(**overrides: Any) -> ResearchRuntimeProfile:
    """A synthetic operator profile: invented digests, an offline fixture agent, no egress, no secrets."""
    data: dict[str, Any] = {"profile_id": "agsci.synthetic.offline", "upstream_repository": "SakanaAI/AI-Scientist-v3",
                            "upstream_revision": "0" * 40, "image_digest": "1" * 64, "agents": ("fixture-agent",),
                            "egress_hosts": (), "timeout_seconds": 600, "secret_refs": ()}
    data.update(overrides)
    return ResearchRuntimeProfile(**data)

RUN_ID = "20261031_000000"
METRICS = {"packing_density": 0.8125, "runtime_seconds": 12.5}
ALL_PHASES = ("task_draft", "investigation", "ablation", "final_evaluation", "posthoc", "paper")


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True).encode("ascii")


def complete_files() -> dict[str, bytes]:
    return {
        "task/task.md": b"Synthetic task: pack circles into a unit square.\n",
        "investigation/summary.json": _json({"best_score": METRICS["packing_density"]}),
        "ablation/aggregate.json": _json({"ablations": 2}),
        "final_evaluation/result.json": _json({"metrics": METRICS}),
        "posthoc/manifest.json": _json({"figures": 1}),
        "paper/paper.pdf": b"%PDF-1.4 synthetic\n",
    }


def output(**overrides: Any) -> ResearchRunOutput:
    data: dict[str, Any] = {
        "exit_code": 0, "upstream_run_id": RUN_ID,
        "phases": {phase: "COMPLETED" for phase in ALL_PHASES},
        "files": complete_files(),
        "claims": ({"metric": "packing_density", "value": METRICS["packing_density"]},),
        "egress_hosts_contacted": (),
    }
    data.update(overrides)
    return ResearchRunOutput(**data)


def request(**overrides: Any) -> CampaignRequest:
    data: dict[str, Any] = {"agent": "fixture-agent", "phases": ALL_PHASES, "budget_profile": "reduced",
                            "require_final_evaluation": True}
    data.update(overrides)
    return CampaignRequest(**data)


class ScriptedRuntime:
    provider_id = "synthetic.research-runtime"

    def __init__(self, result: Any = None, *, available: bool = True, raises: BaseException | None = None) -> None:
        self.result = output() if result is None else result
        self._available, self._raises = available, raises
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def run(self, campaign: CampaignRequest, files_budget: Mapping[str, int]) -> object:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self.result


UNAVAILABLE = ResearchRuntimeUnavailable("docker daemon not reachable")
TIMEOUT = ResearchRuntimeTimeout("wall clock exceeded")
