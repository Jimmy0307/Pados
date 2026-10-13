# SPDX-License-Identifier: AGPL-3.0-only
"""External-runtime adapters for the capability loop (REA discovery, OpenMAIC learning).

Each adapter keeps its provider's response schema behind it, fails closed when the runtime is absent,
unhealthy, unauthorized or incompatible, and emits only the provider-neutral contracts of this package.
"""
