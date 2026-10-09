# SPDX-License-Identifier: AGPL-3.0-only
"""Provider-independent, versioned contracts for cross-resource task completion.

These contracts describe intent, results, and rebuildable projections. They do
not grant authority, schedule work, execute providers, verify results, or commit
semantic state.
"""

from pados.work_protocol.models import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityRecommendation,
    CoverageMap,
    Goal,
    GoalRelation,
    ResultCandidate,
    ResidualTask,
    TaskEnvelope,
    WorkAssignment,
    WorkGraph,
)

__all__ = [
    "CapabilityRequest",
    "CapabilityResult",
    "CapabilityRecommendation",
    "CoverageMap",
    "Goal",
    "GoalRelation",
    "ResultCandidate",
    "ResidualTask",
    "TaskEnvelope",
    "WorkAssignment",
    "WorkGraph",
]
