# SPDX-License-Identifier: AGPL-3.0-only
from pados.core.resource_guard import ResourceLimits


def can_start_local_llm(active_jobs: int, limits: ResourceLimits | None = None) -> bool:
    active_limits = limits or ResourceLimits()
    return active_jobs < active_limits.max_local_llm_concurrency

