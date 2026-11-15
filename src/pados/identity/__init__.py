# SPDX-License-Identifier: AGPL-3.0-only
"""Unified Identity & Session, phase 1: device pairing and server-side browser sessions.

The trust anchor stays the control token. Only its holder can mint a pairing code, and a session grants exactly the
control-token authority on the control plane, never more. See ``docs/identity/UNIFIED_IDENTITY_SESSION_DESIGN.md``.
"""
