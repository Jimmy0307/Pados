# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS Resource / Hardware-Aware Planning (descriptive feasibility, never authority).

Hardware profiles say what a machine has; model runtime profiles say what a model needs. The inventory
answers "does this requirement fit this resource, and why" deterministically, and the planner turns a
task's capability into explained (runtime, model, resource) tuples through the Capability Fabric's
``ResourceInventory`` seam only.

    described != registered != authorized != selected != accepted

Nothing here probes hardware or the network, installs or selects a model, ranks providers (the Worker
Router ``pados.worker.router.select_worker`` stays the only router), or authorizes anything: a feasible
tuple is input to ``pados.capability.policy.evaluate(resource_fit=...)`` and nothing more.
"""
