# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceLimits:
    max_local_llm_concurrency: int = 1
    max_deterministic_workers: int = 4
    max_active_worktrees: int = 2

