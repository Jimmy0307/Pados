# SPDX-License-Identifier: AGPL-3.0-only
"""OP-004: every mutating core API route declares its idempotency or reconciliation contract.

A route whose contract is IDEMPOTENCY_KEY must really take an idempotency key. A new mutating route without an
inventory entry, or an entry for a route that no longer exists, fails here.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterator

import pytest
from fastapi.routing import APIRoute

from pados.operability.mutations import MUTATIONS, PAYLOAD_KEY_ROUTES, MutationKind

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@pytest.fixture(scope="module")
def routes() -> Iterator[dict[tuple[str, str], APIRoute]]:
    import os

    from pados.api.app import create_app
    from pados.config import get_settings

    previous = os.environ.get("PADOS_CONTROL_API_TOKEN_HASH")
    os.environ["PADOS_CONTROL_API_TOKEN_HASH"] = hashlib.sha256(b"op004-inventory").hexdigest().upper()
    get_settings.cache_clear()
    try:
        app = create_app()
        yield {(method, route.path): route for route in app.routes if isinstance(route, APIRoute)
               for method in route.methods & MUTATING}
    finally:
        if previous is None:
            os.environ.pop("PADOS_CONTROL_API_TOKEN_HASH", None)
        else:
            os.environ["PADOS_CONTROL_API_TOKEN_HASH"] = previous
        get_settings.cache_clear()


def test_every_mutating_route_is_classified_and_no_entry_is_stale(routes: dict[tuple[str, str], APIRoute]) -> None:
    assert sorted(set(routes) - set(MUTATIONS)) == []  # unclassified new routes
    assert sorted(set(MUTATIONS) - set(routes)) == []  # entries for removed routes


def test_idempotency_key_routes_really_take_a_key(routes: dict[tuple[str, str], APIRoute]) -> None:
    for key, kind in MUTATIONS.items():
        if kind is not MutationKind.IDEMPOTENCY_KEY or key in PAYLOAD_KEY_ROUTES:
            continue
        body = routes[key].body_field
        assert body is not None, key
        annotation = getattr(body.field_info, "annotation", None)
        assert "idempotency_key" in getattr(annotation, "model_fields", {}), key
