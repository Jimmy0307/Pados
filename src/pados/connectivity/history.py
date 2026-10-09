# SPDX-License-Identifier: AGPL-3.0-only
"""Connectivity history (DataKind.CONNECTIVITY_HISTORY, R1_OPERATIONAL).

Stores state transitions only: ``{link, from, to, at}``. No probe target,
SSID, public/mobile IP, credential or overlay key is ever recorded. The file is
bounded by entry count and by the R1 retention window. Retention is enforced on
every write, on every read (``entries`` never returns expired or malformed
rows and removes them from disk), at runtime startup and on each background probe tick (``prune``). All
writes are reload + modify + persist under the cross-process store lock. It is
operational evidence, never semantic or audit state.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pados.connectivity.state import ConnectivityState, Link
from pados.connectivity.storage import assert_record_schema, atomic_write_json, read_json, store_lock
from pados.lifecycle.retention import DataKind, RetentionPolicy, policy_for

ENTRY_KEYS = ("link", "from", "to", "at")
MAX_ENTRIES_LIMIT = 10_000
_LINKS = frozenset(link.value for link in Link)
_STATES = frozenset(state.value for state in ConnectivityState)
_CLOCK_SKEW = timedelta(minutes=5)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ConnectivityHistory:
    def __init__(self, path: Path, *, max_entries: int = 500, retention: timedelta | None = None,
                 clock: Callable[[], datetime] = _utc_now) -> None:
        if not 1 <= max_entries <= MAX_ENTRIES_LIMIT:
            raise ValueError("CONNECTIVITY_HISTORY_MAX_ENTRIES_OUT_OF_RANGE")
        window = RetentionPolicy().r1
        self.path = Path(path)
        self.max_entries = max_entries
        self.retention = min(retention, window) if retention is not None else window
        self._clock = clock

    def _valid(self, row: object, cutoff: datetime, now: datetime) -> bool:
        if not isinstance(row, dict) or set(row) != set(ENTRY_KEYS):
            return False
        link, before, after, stamp = row["link"], row["from"], row["to"], row["at"]
        if not all(isinstance(value, str) for value in (link, before, after, stamp)):
            return False  # type-check before membership: unhashable JSON values never raise
        if link not in _LINKS or before not in _STATES or after not in _STATES or len(stamp) > 64:
            return False
        try:
            at = datetime.fromisoformat(stamp)
        except ValueError:
            return False
        return at.tzinfo is not None and cutoff <= at <= now + _CLOCK_SKEW

    def _filtered(self, rows: object) -> list[dict[str, Any]]:
        now = self._clock()
        cutoff = now - self.retention
        kept = [row for row in rows if self._valid(row, cutoff, now)] if isinstance(rows, list) else []
        return kept[-self.max_entries:]

    def _load(self) -> tuple[list[dict[str, Any]], int]:
        data = read_json(self.path)
        rows = data.get("entries") if isinstance(data, dict) else None
        # A corrupt document counts as one bad row so the next locked read rewrites it.
        total = len(rows) if isinstance(rows, list) else (1 if self.path.exists() else 0)
        return self._filtered(rows), total

    def _persist(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            assert_record_schema(row, ENTRY_KEYS)
        atomic_write_json(self.path, {"kind": DataKind.CONNECTIVITY_HISTORY.value,
                                      "retention_class": policy_for(DataKind.CONNECTIVITY_HISTORY).retention.value,
                                      "entries": rows})

    def entries(self) -> list[dict[str, Any]]:
        """Retained entries only. Expired, malformed or over-count rows are never returned and
        are also removed from disk (under the store lock) by this read."""
        if not self.path.exists():
            return []
        with store_lock(self.path):
            rows, total = self._load()
            if total != len(rows):
                self._persist(rows)
            return rows

    def prune(self) -> int:
        """Rewrite the file without expired/malformed rows; returns how many were removed."""
        if not self.path.exists():
            return 0
        with store_lock(self.path):
            rows, total = self._load()
            if total != len(rows):
                self._persist(rows)
            return total - len(rows)

    def record(self, link: Link, previous: ConnectivityState, current: ConnectivityState, at: datetime) -> None:
        entry = {"link": link.value, "from": previous.value, "to": current.value, "at": at.isoformat()}
        assert_record_schema(entry, ENTRY_KEYS)
        with store_lock(self.path):
            rows, _total = self._load()
            self._persist(self._filtered([*rows, entry]))
