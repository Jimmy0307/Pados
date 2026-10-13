# SPDX-License-Identifier: AGPL-3.0-only
"""Connectivity state machine and projection (operational, not semantic)."""
from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from pados.connectivity.storage import atomic_write_json, store_lock


class ConnectivityState(StrEnum):
    CONNECTED = "CONNECTED"
    INTERMITTENT = "INTERMITTENT"
    OFFLINE = "OFFLINE"
    RECOVERING = "RECOVERING"
    UNKNOWN = "UNKNOWN"


class Link(StrEnum):
    WAN = "WAN"  # general Internet egress (cloud providers, push)
    REMOTE_TRANSPORT = "REMOTE_TRANSPORT"  # private overlay used by remote clients


Clock = Callable[[], datetime]
Probe = Callable[[], bool]
# (link, from_state, to_state, at): fired after a state change, outside the lock.
TransitionHook = Callable[[Link, ConnectivityState, ConnectivityState, datetime], None]


def _utc_now() -> datetime:
    return datetime.now(UTC)


# Tolerated clock skew for observation timestamps. A timestamp further in the
# future, timezone-naive or unparseable has no trustworthy age: it is stale.
CLOCK_SKEW = timedelta(seconds=60)


def observation_age_seconds(observed_at: object, now: datetime) -> float | None:
    """Age of an observation, or ``None`` (=> stale) when it cannot be trusted."""
    if isinstance(observed_at, datetime):
        stamp = observed_at
    elif isinstance(observed_at, str) and len(observed_at) <= 64:
        try:
            stamp = datetime.fromisoformat(observed_at)
        except ValueError:
            return None
    else:
        return None
    if stamp.tzinfo is None or stamp.utcoffset() is None or now.tzinfo is None:
        return None
    age = (now - stamp).total_seconds()
    if age < -CLOCK_SKEW.total_seconds():
        return None
    return max(0.0, age)


@dataclass
class LinkStatus:
    link: Link
    state: ConnectivityState = ConnectivityState.UNKNOWN
    last_connected: str | None = None
    last_disconnected: str | None = None
    recovered_at: str | None = None
    consecutive_ok: int = 0
    transitions: deque[datetime] = field(default_factory=lambda: deque(maxlen=32))

    def to_dict(self) -> dict[str, Any]:
        return {
            "link": self.link.value,
            "state": self.state.value,
            "last_connected": self.last_connected,
            "last_disconnected": self.last_disconnected,
            "recovered_at": self.recovered_at,
        }


@dataclass(frozen=True)
class Observation:
    """An atomic view of one monitor observation (operational, not semantic)."""

    snapshot: dict[str, Any]
    states: Mapping[Link, ConnectivityState]
    age_seconds: float | None  # None: never observed or untrusted timestamp => stale
    sequence: int

    def state(self, link: Link) -> ConnectivityState:
        return self.states.get(link, ConnectivityState.UNKNOWN)

    def probed(self, link: Link) -> bool:
        return link in self.states


class ConnectivityMonitor:
    """Derives per-link state from probe observations.

    * failure -> OFFLINE, or INTERMITTENT when the link flapped ``flap_threshold``
      times within ``flap_window``;
    * first success after OFFLINE/INTERMITTENT -> RECOVERING;
    * ``stable_after`` consecutive successes -> CONNECTED.
    The snapshot is persisted (optional) as an operational projection file only.

    Locking: ``_observe_lock`` serializes whole observations (probe, apply,
    persist, transition hooks), so persistence happens in observation order;
    ``_state_lock`` guards link state only and is held briefly, so readers get
    a coherent projection without waiting for slow probes.
    """

    def __init__(
        self,
        probes: Mapping[Link, Probe],
        *,
        clock: Clock = _utc_now,
        stable_after: int = 2,
        flap_threshold: int = 3,
        flap_window: timedelta = timedelta(minutes=10),
        snapshot_path: Path | None = None,
        on_transition: TransitionHook | None = None,
    ) -> None:
        self._probes = dict(probes)
        self._clock = clock
        self._stable_after = stable_after
        self._flap_threshold = flap_threshold
        self._flap_window = flap_window
        self._snapshot_path = snapshot_path
        self._on_transition = on_transition
        self._observe_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._observed_at: datetime | None = None
        self._sequence = 0
        self.links = {link: LinkStatus(link) for link in self._probes}

    def _apply(self, status: LinkStatus, ok: bool, now: datetime) -> None:
        stamp = now.isoformat()
        previous = status.state
        if ok:
            status.consecutive_ok += 1
            status.last_connected = stamp
            if previous in {ConnectivityState.OFFLINE, ConnectivityState.INTERMITTENT}:
                status.state = ConnectivityState.RECOVERING
                status.recovered_at = stamp
                status.transitions.append(now)
            elif previous is ConnectivityState.UNKNOWN:
                status.state = ConnectivityState.CONNECTED
            if status.state is ConnectivityState.RECOVERING and status.consecutive_ok >= self._stable_after:
                status.state = ConnectivityState.CONNECTED
            return
        status.consecutive_ok = 0
        if previous is not ConnectivityState.OFFLINE and previous is not ConnectivityState.INTERMITTENT:
            status.last_disconnected = stamp
            status.transitions.append(now)
        recent = [t for t in status.transitions if now - t <= self._flap_window]
        status.state = (ConnectivityState.INTERMITTENT if len(recent) >= self._flap_threshold
                        else ConnectivityState.OFFLINE)

    @property
    def probe_count(self) -> int:
        return len(self._probes)

    def observe(self) -> dict[str, Any]:
        with self._observe_lock:
            now = self._clock()
            results = {}
            for link, probe in self._probes.items():
                try:
                    results[link] = bool(probe())
                except OSError:
                    results[link] = False
            changes: list[tuple[Link, ConnectivityState, ConnectivityState]] = []
            with self._state_lock:
                for link, ok in results.items():
                    status = self.links[link]
                    previous = status.state
                    self._apply(status, ok, now)
                    if status.state is not previous:
                        changes.append((link, previous, status.state))
                self._observed_at = now
                self._sequence += 1
                snapshot = self._projection_locked()
            if self._snapshot_path is not None:
                write_snapshot(self._snapshot_path, snapshot)
            if self._on_transition is not None:
                for link, previous, current in changes:
                    self._on_transition(link, previous, current, now)
            return snapshot

    def state(self, link: Link) -> ConnectivityState:
        with self._state_lock:
            status = self.links.get(link)
            return status.state if status else ConnectivityState.UNKNOWN

    def wan_available(self) -> bool:
        return self.state(Link.WAN) in {ConnectivityState.CONNECTED, ConnectivityState.RECOVERING}

    def observation_age(self) -> float | None:
        """Seconds since the last completed observation (monitor clock); ``None`` if never observed."""
        with self._state_lock:
            observed = self._observed_at
        return None if observed is None else observation_age_seconds(observed, self._clock())

    def _projection_locked(self) -> dict[str, Any]:
        return {
            "links": {link.value: status.to_dict() for link, status in self.links.items()},
            "observed_at": self._observed_at.isoformat() if self._observed_at else None,
            "sequence": self._sequence,
            "home_base": "RUNNING",
            "semantic_authority": "NONE",
            "affects_job_state": False,
        }

    def projection(self) -> dict[str, Any]:
        with self._state_lock:
            return self._projection_locked()

    def observation(self) -> Observation:
        """One coherent read: projection, link states and age all from the same snapshot,
        taken under the state lock. Callers never combine state and age from separate reads."""
        with self._state_lock:
            snapshot = self._projection_locked()
            age = None if self._observed_at is None else observation_age_seconds(self._observed_at, self._clock())
            states = {link: status.state for link, status in self.links.items()}
        return Observation(snapshot=snapshot, states=states, age_seconds=age, sequence=snapshot["sequence"])


def freshness(age: float | None, stale_after_seconds: float) -> dict[str, Any]:
    """Observation age and staleness; never-observed data is stale."""
    return {"observation_age_seconds": None if age is None else round(age, 1),
            "stale": age is None or age > stale_after_seconds}


def _stamp(snapshot: Mapping[str, Any]) -> datetime | None:
    value = snapshot.get("observed_at")
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo is not None and stamp.utcoffset() is not None else None


def write_snapshot(path: Path, snapshot: dict[str, Any]) -> bool:
    """Persist ``snapshot`` unless the file already holds a newer, plausible observation.

    A stored timestamp beyond wall clock + skew (future-dated) is not plausible and
    never blocks newer writes."""
    with store_lock(path):
        existing = load_snapshot(path)
        ours, theirs = _stamp(snapshot), _stamp(existing) if existing else None
        if ours is not None and theirs is not None and theirs > ours \
                and theirs <= max(ours, datetime.now(UTC)) + CLOCK_SKEW:
            return False
        # ``sequence`` is in-process ordering only; the persisted schema stays unchanged.
        atomic_write_json(path, {key: value for key, value in snapshot.items() if key != "sequence"})
        return True


def load_snapshot(path: str | Path) -> dict[str, Any] | None:
    """Read a persisted projection; stale or missing data is reported, never invented."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
