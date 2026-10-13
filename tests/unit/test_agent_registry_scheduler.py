import pytest

from pados.agents.registry import REQUIRED_ROLES, load_agent_registry, role_can_write
from pados.core.dependency_graph import topological_sort
from pados.core.resource_guard import ResourceLimits
from pados.core.resource_scheduler import SchedulerState, can_schedule


def test_loads_eight_agent_registry_and_reviewer_is_read_only() -> None:
    registry = load_agent_registry()
    assert set(registry) == REQUIRED_ROLES
    assert role_can_write("builder", registry) is True
    assert role_can_write("reviewer", registry) is False


def test_dependency_graph_orders_upstream_first() -> None:
    ordered = topological_sort({"build": {"plan"}, "qa": {"build"}, "plan": set()})
    assert ordered.index("plan") < ordered.index("build") < ordered.index("qa")


def test_dependency_graph_rejects_cycle() -> None:
    with pytest.raises(ValueError):
        topological_sort({"a": {"b"}, "b": {"a"}})


def test_resource_scheduler_limits_concurrency() -> None:
    limits = ResourceLimits()
    assert can_schedule(SchedulerState(active_local_llm=0), worker_type="local_llm", limits=limits)
    assert not can_schedule(SchedulerState(active_local_llm=1), worker_type="local_llm", limits=limits)
    assert can_schedule(
        SchedulerState(active_deterministic=3), worker_type="deterministic", limits=limits
    )
    assert not can_schedule(
        SchedulerState(active_deterministic=4), worker_type="deterministic", limits=limits
    )
    assert not can_schedule(SchedulerState(), worker_type="worktree", limits=limits, available_ram_gb=5)

