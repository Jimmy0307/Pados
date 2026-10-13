# SPDX-License-Identifier: AGPL-3.0-only
"""P7 capability loop: provider-neutral contracts that connect external runtimes to PADOS.

External runtimes (a capability discovery provider, an adaptive learning provider) supply observations and
evidence. They never become authority: nothing in this package authorizes, schedules, routes model work,
commits semantic state, writes Core receipts or promotes a capability. Every record it produces declares
``semantic_authority == NONE``.
"""
