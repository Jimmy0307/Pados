# SPDX-License-Identifier: AGPL-3.0-only
"""PADOS Local Compute Node V1.

A compute node is a replaceable execution substrate. It executes bounded jobs
and returns immutable artifacts; it is never semantic, project, memory, audit,
scheduler or provider-router authority. PADOS verifies every node result
independently and accepts state only through ExecutionReceipt -> validation ->
ResultReceipt -> ScopeVersion CAS -> audit.
"""
