# SPDX-License-Identifier: AGPL-3.0-only
"""Outbound diagnostics are rate limited (final review MEDIUM-2): a session may call diagnostics, and each call makes
outbound requests (and a chat probe spends inference), so a runaway or hostile client must not turn the route into a
request amplifier. Per provider: a burst of connection checks, then a steady refill; chat probes have a smaller budget;
a process-wide budget caps the sum. A refused call makes no outbound request and leaves the last result untouched."""
from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_provider_diagnostics import Env, make_env  # noqa: E402

from pados.providers.diagnostics import (  # noqa: E402
    CHECK_BURST,
    CHECK_REFILL_SECONDS,
    GLOBAL_BURST,
    PROBE_BURST,
    PROBE_REFILL_SECONDS,
    RATE_LIMITED,
    TokenBucket,
)
from pados.providers.setup import SetupError  # noqa: E402


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    yield from make_env(tmp_path)


def _seconds(clock: object, seconds: float) -> None:
    clock.now = clock.now + timedelta(seconds=seconds)  # type: ignore[attr-defined]


def test_bucket_burst_then_refill() -> None:
    bucket = TokenBucket(capacity=3, refill_seconds=10.0)
    assert [bucket.take(0.0) for _ in range(4)] == [True, True, True, False]
    assert bucket.take(9.9) is False
    assert bucket.take(10.0) is True
    assert bucket.take(10.0) is False
    assert bucket.take(1000.0) and bucket.take(1000.0) and bucket.take(1000.0)  # refill never exceeds capacity
    assert bucket.take(1000.0) is False


def test_bucket_tolerates_a_clock_that_goes_backwards() -> None:
    bucket = TokenBucket(capacity=1, refill_seconds=10.0)
    assert bucket.take(100.0) is True
    assert bucket.take(50.0) is False
    assert bucket.take(110.0) is True


def test_connection_checks_are_limited_per_provider_and_refill(env: Env) -> None:
    diagnostics = env.diagnostics()
    for _ in range(CHECK_BURST):
        assert diagnostics.diagnose("ollama-local")["reachability"]["state"] == "REACHABLE"
    last = diagnostics.overview()
    with pytest.raises(SetupError) as refused:
        diagnostics.diagnose("ollama-local")
    assert (refused.value.code, refused.value.status) == (RATE_LIMITED, 429)
    assert diagnostics.overview() == last  # the refused call changed nothing
    _seconds(env.clock, CHECK_REFILL_SECONDS)
    assert diagnostics.diagnose("ollama-local")["reachability"]["state"] == "REACHABLE"


def test_a_refused_call_makes_no_outbound_request(env: Env) -> None:
    calls: list[str] = []
    diagnostics = env.diagnostics()
    real = diagnostics._describe

    def counting(target, now):  # type: ignore[no-untyped-def]
        calls.append(target.runtime_id)
        return real(target, now)

    diagnostics._describe = counting
    for _ in range(CHECK_BURST):
        diagnostics.diagnose("ollama-local")
    with pytest.raises(SetupError):
        diagnostics.diagnose("ollama-local")
    assert len(calls) == CHECK_BURST


def test_disabled_and_unknown_providers_cost_nothing(env: Env) -> None:
    env.setup(enabled=False)
    diagnostics = env.diagnostics()
    for _ in range(CHECK_BURST * 2):
        assert diagnostics.diagnose("ollama-local")["reachability"]["state"] == "DISABLED"
    with pytest.raises(SetupError) as missing:
        diagnostics.diagnose("no-such-provider")
    assert missing.value.status == 404


def test_the_limits_leave_room_for_the_documented_operator_flow() -> None:
    # providers-live.spec.ts: check, probe, check twice to open the circuit, check to recover = 5 calls, 1 probe
    assert CHECK_BURST >= 5 and PROBE_BURST >= 1 and GLOBAL_BURST >= CHECK_BURST
    assert PROBE_BURST < CHECK_BURST and PROBE_REFILL_SECONDS > CHECK_REFILL_SECONDS



RESERVED = ["PADOS_DB_PASSWORD", "PADOS_CONTROL_API_TOKEN_HASH", "PADOS_DB_HOST", "PADOS_CONTROL_TOKEN",
            "OLLAMA_BASE_URL", "PADOS_IDENTITY_SESSIONS_ENABLED", "pados_db_password"]


@pytest.mark.parametrize("name", RESERVED)
def test_a_provider_key_reference_may_not_name_a_pados_setting(name: str) -> None:
    """Security review LOW-2: a provider's key is read from the named environment variable and sent to its host, so
    naming one of PADOS's own settings (the database password, the control token hash) must be refused."""
    from pados.providers.setup import parse_settings

    with pytest.raises(SetupError) as refused:
        parse_settings({"provider_id": "lan", "kind": "ollama", "endpoint": "http://127.0.0.1:1", "auth_env": name})
    assert refused.value.code in ("PROVIDER_AUTH_ENV_RESERVED", "PROVIDER_AUTH_ENV_NAME_INVALID")


@pytest.mark.parametrize("name", RESERVED[:-1])
def test_a_gateway_contract_reference_may_not_name_a_pados_setting(name: str) -> None:
    from pydantic import ValidationError

    from pados.compute_node.gateway_contract import GatewayAuth

    with pytest.raises(ValidationError, match="GATEWAY_AUTH_REFERENCE_RESERVED"):
        GatewayAuth(scheme="bearer", auth_ref=f"secretref://env/{name}")


def test_a_dedicated_key_name_is_still_accepted() -> None:
    from pados.compute_node.gateway_contract import GatewayAuth
    from pados.providers.setup import parse_settings

    item = parse_settings({"provider_id": "lan", "kind": "ollama", "endpoint": "http://127.0.0.1:1",
                           "auth_env": "PADOS_SIM_GATEWAY_KEY"})
    assert item.auth_env == "PADOS_SIM_GATEWAY_KEY"
    assert GatewayAuth(scheme="bearer", auth_ref="secretref://env/PADOS_SIM_GATEWAY_KEY").auth_ref
