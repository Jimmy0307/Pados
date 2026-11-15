# SPDX-License-Identifier: AGPL-3.0-only
"""Unified Identity & Session, phase 2: a replaceable OIDC relying party (design §4).

One generic Authorization Code + PKCE S256 implementation with presets for Microsoft Entra ID and Google. ID tokens
are verified in pure Python (RS256 only). A successful sign-in grants nothing by itself: only an explicit actor
binding (``pados.identity.actors``) yields an actor session, and an actor session never passes the control plane.
Real Entra ID and Google tenants are NOT_LIVE_VALIDATED.
"""
