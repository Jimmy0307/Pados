# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS Sandbox Fabric Foundation V1.

A sandbox is an isolation substrate: it runs bounded work in an ephemeral
workspace and returns artifacts. It never holds semantic authority; results
enter PADOS only through receipt -> validation -> CAS. V1 ships policy
contracts and a deterministic simulated adapter; live hypervisor enforcement
is a later UAT.
"""
