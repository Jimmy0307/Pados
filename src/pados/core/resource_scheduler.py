# SPDX-License-Identifier: AGPL-3.0-only
from dataclasses import dataclass

from pados.core.resource_guard import ResourceLimits


@dataclass
class SchedulerState:
    active_local_llm: int = 0
    active_deterministic: int = 0
    active_worktrees: int = 0
    paused: bool = False


def can_schedule(
    state: SchedulerState,
    *,
    worker_type: str,
    limits: ResourceLimits,
    available_ram_gb: float = 32.0,
) -> bool:
    if state.paused or available_ram_gb < 6:
        return False
    if worker_type == "local_llm":
        return state.active_local_llm < limits.max_local_llm_concurrency
    if worker_type == "deterministic":
        return state.active_deterministic < limits.max_deterministic_workers
    if worker_type == "worktree":
        return state.active_worktrees < limits.max_active_worktrees
    return False

