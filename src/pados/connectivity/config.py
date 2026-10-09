# SPDX-License-Identifier: AGPL-3.0-only
"""Optional connectivity configuration (``PADOS_CONNECTIVITY_CONFIG``), doctor and
Mission Control projections. Unset means "not configured" and never affects Core.

Config JSON (operator file only; probe targets are never accepted from an API)::

    {"mode": "INTERMITTENT_WAN_HOME_BASE",
     "wan_probe": {"host": "<public literal IP>", "port": 443},
     "overlay_probe": {"host": "<overlay peer literal IP>", "port": 8089},
     "snapshot_path": "<runtime dir>/connectivity.json",
     "history_path": "<runtime dir>/connectivity-history.json",
     "stale_after_seconds": 300,
     "background_probe": {"enabled": true, "interval_seconds": 60,
                          "timeout_seconds": 3, "max_backoff_seconds": 600},
     "provider_requirements": {"<provider_id>": "<NetworkRequirement>"}}

Invalid probe targets or background settings make the whole file
CONNECTIVITY_CONFIG_INVALID: nothing is probed.
"""
from __future__ import annotations

import json
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pados.connectivity.background import BackgroundProbeConfigError, BackgroundProbeSettings
from pados.connectivity.state import (
    Clock,
    ConnectivityMonitor,
    ConnectivityState,
    Link,
    Probe,
    TransitionHook,
    freshness,
    load_snapshot,
    observation_age_seconds,
)
from pados.connectivity.targets import ProbeTargetError, validate_probe_target

CONNECTIVITY_CONFIG_INVALID = "CONNECTIVITY_CONFIG_INVALID"
DEFAULT_PROBE_TIMEOUT = 1.5
_PROVIDER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")


class ConnectivityMode(StrEnum):
    ALWAYS_ON_WAN = "ALWAYS_ON_WAN"
    INTERMITTENT_WAN_HOME_BASE = "INTERMITTENT_WAN_HOME_BASE"
    PHONE_HOTSPOT_TEMPORARY_WAN = "PHONE_HOTSPOT_TEMPORARY_WAN"
    LAN_ONLY = "LAN_ONLY"


class ConnectivityConfigError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"{CONNECTIVITY_CONFIG_INVALID}:{reason}")
        self.code = CONNECTIVITY_CONFIG_INVALID
        self.reason = reason


def tcp_probe(host: str, port: int, timeout: float = DEFAULT_PROBE_TIMEOUT) -> Callable[[], bool]:
    def probe() -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    return probe


# ``factory(host, port, timeout) -> Probe``; tests inject a deterministic one.
ProbeFactory = Callable[[str, int, float], Probe]


@dataclass(frozen=True)
class ConnectivityConfig:
    mode: ConnectivityMode
    wan_probe: tuple[str, int] | None
    overlay_probe: tuple[str, int] | None
    snapshot_path: Path | None
    stale_after_seconds: int = 300
    background: BackgroundProbeSettings | None = None
    history_path: Path | None = None
    provider_requirements: tuple[tuple[str, object], ...] = ()

    @property
    def background_enabled(self) -> bool:
        return self.background is not None and self.background.enabled

    def operator_requirement(self, provider_id: str) -> object | None:
        """Raw operator metadata for ``provider_id`` (validated where it is used)."""
        for pid, value in self.provider_requirements:
            if pid == provider_id:
                return value
        return None

    @classmethod
    def load(cls, path: str) -> ConnectivityConfig:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ConnectivityConfigError("CONFIG_NOT_AN_OBJECT")
        try:
            return cls._parse(data)
        except ConnectivityConfigError:
            raise
        except (ProbeTargetError, BackgroundProbeConfigError) as exc:
            raise ConnectivityConfigError(exc.code) from exc
        except (TypeError, ValueError, KeyError) as exc:
            raise ConnectivityConfigError("CONFIG_FIELD_INVALID") from exc

    @classmethod
    def _parse(cls, data: dict[str, Any]) -> ConnectivityConfig:
        def endpoint(key: str, link: Link) -> tuple[str, int] | None:
            value = data.get(key)
            return validate_probe_target(value, link=link) if value else None

        def optional_path(key: str) -> Path | None:
            value = data.get(key)
            if value in (None, ""):
                return None
            if not isinstance(value, str):
                raise ConnectivityConfigError(f"{key.upper()}_INVALID")
            return Path(value)

        requirements = data.get("provider_requirements") or {}
        if not isinstance(requirements, dict) or not all(
                isinstance(pid, str) and _PROVIDER_ID.fullmatch(pid) for pid in requirements):
            raise ConnectivityConfigError("PROVIDER_REQUIREMENTS_SHAPE_INVALID")
        background = data.get("background_probe")
        snapshot_path = optional_path("snapshot_path")
        history_path = optional_path("history_path")
        if history_path is None and snapshot_path is not None:
            history_path = snapshot_path.with_name(f"{snapshot_path.stem}-history.json")
        stale = data.get("stale_after_seconds", 300)
        if isinstance(stale, bool) or not isinstance(stale, int) or not 1 <= stale <= 86_400:
            raise ConnectivityConfigError("STALE_AFTER_SECONDS_INVALID")
        return cls(
            mode=ConnectivityMode(str(data.get("mode", "INTERMITTENT_WAN_HOME_BASE"))),
            wan_probe=endpoint("wan_probe", Link.WAN),
            overlay_probe=endpoint("overlay_probe", Link.REMOTE_TRANSPORT),
            snapshot_path=snapshot_path,
            stale_after_seconds=stale,
            background=BackgroundProbeSettings.from_mapping(background) if background is not None else None,
            history_path=history_path,
            provider_requirements=tuple(sorted(requirements.items())),
        )

    def monitor(self, *, probe_factory: ProbeFactory | None = None, clock: Clock | None = None,
                on_transition: TransitionHook | None = None) -> ConnectivityMonitor:
        factory = probe_factory or tcp_probe
        timeout = self.background.timeout_seconds if self.background else DEFAULT_PROBE_TIMEOUT
        probes = {}
        if self.wan_probe:
            probes[Link.WAN] = factory(*self.wan_probe, timeout)
        if self.overlay_probe:
            probes[Link.REMOTE_TRANSPORT] = factory(*self.overlay_probe, timeout)
        if clock is not None:
            return ConnectivityMonitor(probes, clock=clock, snapshot_path=self.snapshot_path,
                                       on_transition=on_transition)
        return ConnectivityMonitor(probes, snapshot_path=self.snapshot_path, on_transition=on_transition)


def load_config(config_path: str) -> ConnectivityConfig | dict[str, str]:
    """The parsed config, or the CONNECTIVITY_CONFIG_INVALID projection (fail closed)."""
    try:
        return ConnectivityConfig.load(config_path)
    except ConnectivityConfigError as exc:
        return {"status": CONNECTIVITY_CONFIG_INVALID, "reason": exc.reason}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": CONNECTIVITY_CONFIG_INVALID}


def _persisted_view(snapshot: dict[str, Any], stale_after: int) -> dict[str, Any]:
    """A persisted snapshot with its wall-clock age; future-dated, naive or
    unparseable ``observed_at`` has no age and is therefore stale (F-22)."""
    age = observation_age_seconds(snapshot.get("observed_at"), datetime.now(UTC))
    links = snapshot.get("links")
    return {**snapshot, "links": links if isinstance(links, dict) else {}, **freshness(age, stale_after),
            "probe_mode": "PERSISTED_SNAPSHOT", "probe_thread": None}


def _live_runtime(config_path: str) -> Any:
    from pados.connectivity.runtime import active_runtime

    return active_runtime(config_path)


def connectivity_visibility(config_path: str) -> dict[str, Any]:
    """Mission Control block: the live background projection when this process runs the
    background probe, otherwise the last persisted snapshot; always with observation age,
    probe thread health and an explicit ``stale`` flag."""
    if not config_path:
        return {"status": "CONNECTIVITY_NOT_CONFIGURED", "semantic_authority": "NONE"}
    config = load_config(config_path)
    if not isinstance(config, ConnectivityConfig):
        return {**config, "semantic_authority": "NONE"}
    runtime = _live_runtime(config_path)
    view: dict[str, Any] | None = runtime.view() if runtime is not None else None
    if view is None and config.snapshot_path:
        persisted = load_snapshot(config.snapshot_path)
        view = _persisted_view(persisted, config.stale_after_seconds) if persisted is not None else None
    if view is None:
        return {"status": "CONNECTIVITY_NOT_OBSERVED", "mode": config.mode.value, "semantic_authority": "NONE"}
    return {**view, "status": "CONNECTIVITY_OBSERVED", "mode": config.mode.value,
            "age_seconds": view["observation_age_seconds"]}


def _doctor_view(config_path: str, config: ConnectivityConfig) -> tuple[dict[str, Any], str]:
    runtime = _live_runtime(config_path)
    if runtime is not None:
        view: dict[str, Any] = runtime.view()
        if not view["stale"]:
            return view, "BACKGROUND_PROBE"
        runtime.monitor.observe()  # stale shared state: one bounded refresh, never trusted as-is
        return runtime.view(), "BACKGROUND_PROBE_REFRESHED"
    if config.background_enabled and config.snapshot_path is not None:
        persisted = load_snapshot(config.snapshot_path)
        if persisted is not None:
            view = _persisted_view(persisted, config.stale_after_seconds)
            if not view["stale"]:
                return view, "BACKGROUND_SNAPSHOT"
    monitor = config.monitor()
    monitor.observe()
    observation = monitor.observation()
    return {**observation.snapshot, **freshness(observation.age_seconds, config.stale_after_seconds),
            "probe_mode": "ON_DEMAND", "probe_thread": None}, "DIRECT_PROBE"


def connectivity_status(config_path: str, *, core_ready: bool, compute_nodes: dict[str, Any] | None) -> dict[str, Any]:
    """Doctor section. Distinguishes REMOTE_UNREACHABLE (transport) from PADOS_UNHEALTHY (authority).

    Reachability is never derived from stale data (``remote_status=REMOTE_STATUS_STALE``)."""
    pados = "PADOS_HEALTHY" if core_ready else "PADOS_UNHEALTHY"
    compute = (compute_nodes or {}).get("status", "COMPUTE_NODES_NOT_CONFIGURED")
    base = {"home_base_local_services": pados, "local_compute": compute,
            "local_compute_depends_on_wan": False, "semantic_authority": "NONE"}
    if not config_path:
        return {"status": "CONNECTIVITY_NOT_CONFIGURED", **base}
    config = load_config(config_path)
    if not isinstance(config, ConnectivityConfig):
        return {**config, **base}
    view, source = _doctor_view(config_path, config)
    links = view.get("links")
    links = links if isinstance(links, dict) else {}

    def link_state(link: Link) -> str:
        row = links.get(link.value)
        return str(row.get("state", "NOT_PROBED")) if isinstance(row, dict) else "NOT_PROBED"

    remote = link_state(Link.REMOTE_TRANSPORT)
    wan = link_state(Link.WAN)
    if remote == "NOT_PROBED":
        remote_status = "NOT_PROBED"
    elif view["stale"]:
        remote_status = "REMOTE_STATUS_STALE"
    elif remote in {ConnectivityState.CONNECTED, ConnectivityState.RECOVERING}:
        remote_status = "REMOTE_REACHABLE"
    else:
        remote_status = "REMOTE_UNREACHABLE"
    return {"status": "CONNECTIVITY_CONFIGURED", "mode": config.mode.value, "wan": wan,
            "remote_transport": remote, "remote_status": remote_status, "private_overlay": remote,
            "probe_source": source, "stale": view["stale"],
            "observation_age_seconds": view["observation_age_seconds"], "probe_thread": view["probe_thread"],
            "background_probe": "ENABLED" if config.background_enabled else "DISABLED", **base}
