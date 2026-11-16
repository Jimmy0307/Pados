# SPDX-License-Identifier: AGPL-3.0-only
"""Fixtures for the phase 2 tests (see ``oidc_harness``). No real tenant is contacted."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.identity.identity_support import FakeClock, Harness
from tests.identity.oidc.oidc_harness import ACTORS_CONFIG, ENVIRON, OIDC_CONFIG, OidcHarness, write_json
from tests.identity.oidc.oidc_support import ENTRA_ISSUER, GOOGLE_ISSUER, FakeNetwork, entra_issuer, google_issuer


@pytest.fixture
def make_oidc(make_harness: Callable[..., Harness], tmp_path: Path) -> Callable[..., OidcHarness]:
    def build(*, oidc_config: object | None = OIDC_CONFIG, actors_config: object | None = ACTORS_CONFIG,
              environ: dict[str, str] | None = None, enabled: bool = True,
              network: Callable[[FakeClock], FakeNetwork] | None = None) -> OidcHarness:
        from pados import config
        from pados.identity.oidc.flow import OidcRuntime

        extra = {"PADOS_IDENTITY_OIDC_CONFIG": "", "PADOS_IDENTITY_ACTORS_CONFIG": ""}
        if oidc_config is not None:
            extra["PADOS_IDENTITY_OIDC_CONFIG"] = write_json(tmp_path / "oidc.json", oidc_config)
        if actors_config is not None:
            extra["PADOS_IDENTITY_ACTORS_CONFIG"] = write_json(tmp_path / "actors.json", actors_config)
        clock = FakeClock()
        entra, google = entra_issuer(clock), google_issuer(clock)
        fakes = network(clock) if network is not None else FakeNetwork(entra, google)
        harness = make_harness(enabled=enabled, clock=clock, **extra)
        harness.app.state.pados_oidc = OidcRuntime(config.get_settings(), transport=fakes.transport,
                                                   environ=ENVIRON if environ is None else environ)
        found = {f.issuer: f for f in fakes.issuers}
        return OidcHarness(harness=harness, network=fakes, entra=found.get(ENTRA_ISSUER, entra),
                           google=found.get(GOOGLE_ISSUER, google))

    return build


@pytest.fixture
def oidc(make_oidc: Callable[..., OidcHarness]) -> OidcHarness:
    return make_oidc()


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch, no_database: None) -> list[dict[str, Any]]:
    """Every call that reached the shared work-request creation service."""
    from pados.control import service as control_service

    calls: list[dict[str, Any]] = []

    def create(_session: object, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        return {"id": f"wr-{len(calls)}", **payload}

    monkeypatch.setattr(control_service, "create_work_request", create)
    return calls
