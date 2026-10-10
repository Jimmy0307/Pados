# SPDX-License-Identifier: AGPL-3.0-only
"""Thin, bounded translation of raw Herdr status documents into telemetry records.

The adapter reads a raw document from an injected ``HerdrSource`` only when a
caller invokes ``collect()``: it starts no timer, thread or schedule and holds no
callback into PADOS, so Herdr has no path to start, cancel, route, approve or
commit anything.

No source-controlled free text is copied (CR-06 H01/M05). Session and pane ids
become PADOS keyed digests; the agent becomes a closed ``HerdrAgentKind``;
labels are never read; project / work-package refs come only from the operator
binding map, looked up by the pane's source tag (the tag itself is never kept).
Any field that could hold terminal content (scrollback, buffer, output, lines,
history, screen, content) is dropped and counted; authority and identity claims
are ignored and counted; unknown raw states become UNKNOWN; oversized,
malformed or over-deep documents are refused whole. The machine reference is
bound by PADOS configuration.

Freshness is proven only by a document-level source timestamp (CR-06 H02):
it must be timezone-aware, within the clock-skew tolerance and not older than
``stale_after_seconds`` at collection; otherwise records carry
``source_observed_at = None`` and are never projected live. Diagnostics are a
closed enum (CR-06 M03).

``CommandHerdrSource`` is defined for the live integration (fixed allowlisted
argv, no shell, one monotonic deadline, output cap, isolated process group that
is killed on timeout/cap/exit) but its wire format is unverified
(HERDR_LIVE_UAT = PENDING). The bounded runner is supported on Linux, where
``waitid(..., WNOWAIT)`` keeps the leader unreaped until group cleanup. The primitive
is feature-detected: runtimes without it (CPython 3.12 on macOS) fail closed,
and Windows is disabled until bounded streaming exists.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import ValidationError

from pados.herdr.boundary import (
    IDENTITY_CLAIM_KEYS,
    HerdrAuthorityViolation,
    assert_no_authority,
    is_authority_claim_key,
    normalized_key,
)
from pados.herdr.contract import (
    AGENT_KIND_MAP,
    CLOCK_SKEW,
    MACHINE_REF_PATTERN,
    PADOS_REF_PATTERN,
    SCHEMA_ID,
    SEMANTIC_AUTHORITY_NONE,
    HerdrAgentKind,
    HerdrAgentState,
    HerdrHealth,
    HerdrTelemetryRecord,
    assert_record_minimized,
    parse_aware,
)
from pados.herdr.diagnostics import HerdrCollectionOutcome, HerdrDiagnostic, diagnostic
from pados.lifecycle.minimization import DataMinimizationViolation

MAX_RAW_BYTES = 256 * 1024
MAX_RAW_DEPTH = 16
MAX_SESSIONS = 64
MAX_ENTRIES = 256
MAX_RAW_STATE_CHARS = 32
MAX_RAW_ID_CHARS = 256
MAX_TAG_CHARS = 128
MAX_REFUSAL_CODES = 8
REF_KEY_BYTES = 32
STALE_AFTER_DEFAULT = 120
STALE_AFTER_MAX = 86_400
COMMAND_TIMEOUT_DEFAULT = 5.0
COMMAND_TIMEOUT_MAX = 30.0
_READ_CHUNK = 64 * 1024
_EXIT_POLL_SECONDS = 0.01
_REAP_TIMEOUT = 5.0

TERMINAL_CONTENT_TOKENS: tuple[str, ...] = ("scrollback", "buffer", "output", "lines", "history", "screen",
                                            "content")
# UNVERIFIED raw state names (normalized); everything else is UNKNOWN.
RAW_STATE_MAP: Mapping[str, HerdrAgentState] = {
    "working": HerdrAgentState.WORKING,
    "idle": HerdrAgentState.IDLE,
    "blocked": HerdrAgentState.BLOCKED,
    "waitinginput": HerdrAgentState.WAITING_INPUT,
    "waiting": HerdrAgentState.WAITING_INPUT,
    "exited": HerdrAgentState.EXITED,
}
RAW_HEALTH_MAP: Mapping[str, HerdrHealth] = {
    "healthy": HerdrHealth.HEALTHY,
    "degraded": HerdrHealth.DEGRADED,
    "unreachable": HerdrHealth.UNREACHABLE,
}
# UNVERIFIED document-level snapshot timestamp keys, in priority order.
SOURCE_TIME_KEYS: tuple[str, ...] = ("snapshot_at", "generated_at", "observed_at")
_SESSION_ID_KEYS = ("session_id", "id")
_PANE_ID_KEYS = ("pane_id", "id")
_STATE_KEYS = ("state", "agent_state", "status")
_TRANSITION_KEYS = ("last_transition_at", "state_changed_at")
_TAG_KEYS = ("tag", "project_tag")
_MACHINE_REF = re.compile(MACHINE_REF_PATTERN)
_PADOS_REF = re.compile(PADOS_REF_PATTERN)
# The command child gets a minimal environment: no PADOS settings or credentials.
_CHILD_ENV_KEYS = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "XDG_RUNTIME_DIR", "TMPDIR", "TEMP", "TMP")


class HerdrSourceError(OSError):
    """A source failure; ``code`` is always a ``HerdrDiagnostic`` (never free text)."""

    def __init__(self, code: object) -> None:
        self.code = diagnostic(code)
        super().__init__(self.code.value)


class HerdrSourceConfigError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class HerdrSource(Protocol):
    def fetch(self) -> bytes:
        """The raw status document bytes (at most the cap plus one byte)."""
        ...

    def installed(self) -> bool:
        """Whether the source is present, decided without executing anything.

        Presence is not runtime availability: that is the latest collection outcome."""
        ...


class FixtureHerdrSource:
    """A raw document read from a local JSON file (tests, demos, offline UAT)."""

    def __init__(self, path: str | Path, *, max_bytes: int = MAX_RAW_BYTES) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes

    def installed(self) -> bool:
        return self.path.is_file() and not self.path.is_symlink()

    def fetch(self) -> bytes:
        if not self.installed():
            raise HerdrSourceError(HerdrDiagnostic.FIXTURE_UNAVAILABLE)
        try:
            with self.path.open("rb") as handle:
                return handle.read(self.max_bytes + 1)
        except OSError as exc:
            raise HerdrSourceError(HerdrDiagnostic.FIXTURE_UNREADABLE) from exc


HERDR_EXECUTABLE = "herdr"
# UNVERIFIED: the live status subcommand is confirmed only by HERDR_LIVE_UAT.
HERDR_STATUS_ARGS: tuple[str, ...] = ("status", "--json")
ALLOWED_COMMAND_ARGS: frozenset[tuple[str, ...]] = frozenset({HERDR_STATUS_ARGS})


def _child_env() -> dict[str, str]:
    return {key: os.environ[key] for key in _CHILD_ENV_KEYS if key in os.environ}


def bounded_run(argv: Sequence[str], *, timeout_seconds: float, max_bytes: int) -> bytes:
    """Run ``argv`` without a shell; return at most ``max_bytes + 1`` bytes of stdout.

    One monotonic deadline covers reading and waiting. On supported Linux, the
    child runs in its own session / process group; the whole group is killed on
    timeout, on crossing the output cap and after the leader exits, so
    descendants never outlive the call. Platforms without non-reaping waitid
    fail closed. Windows is explicitly unsupported until bounded streaming
    exists.
    """
    if sys.platform == "win32":
        raise HerdrSourceError(HerdrDiagnostic.COMMAND_SOURCE_UNSUPPORTED_ON_WINDOWS)
    else:
        waitid_primitives = _waitid_primitives()
        if waitid_primitives is None:
            raise HerdrSourceError(HerdrDiagnostic.SOURCE_UNAVAILABLE)
        return _run_posix(argv, timeout_seconds=timeout_seconds, max_bytes=max_bytes,
                          waitid_primitives=waitid_primitives)


class _WaitIdInfo(Protocol):
    si_pid: int


_WaitId = Callable[[int, int, int], _WaitIdInfo | None]
_WaitIdPrimitives = tuple[_WaitId, int, int, int, int]


def _waitid_primitives() -> _WaitIdPrimitives | None:
    """Return the non-reaping wait primitive only when the runtime exposes all of it."""
    waitid = getattr(os, "waitid", None)
    p_pid = getattr(os, "P_PID", None)
    w_exited = getattr(os, "WEXITED", None)
    w_nohang = getattr(os, "WNOHANG", None)
    w_nowait = getattr(os, "WNOWAIT", None)
    if not callable(waitid) or not all(isinstance(value, int) for value in
                                      (p_pid, w_exited, w_nohang, w_nowait)):
        return None
    return (cast(_WaitId, waitid), cast(int, p_pid), cast(int, w_exited), cast(int, w_nohang), cast(int, w_nowait))


if sys.platform != "win32":
    def _run_posix(argv: Sequence[str], *, timeout_seconds: float, max_bytes: int,
                   waitid_primitives: _WaitIdPrimitives) -> bytes:
        deadline = time.monotonic() + timeout_seconds
        process = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, shell=False, close_fds=True, env=_child_env(),
                                   start_new_session=True)
        try:
            data = _read_bounded(process, deadline, max_bytes)
            if len(data) > max_bytes:
                return data
            if not _exited_unreaped(process.pid, deadline, waitid_primitives):
                raise HerdrSourceError(HerdrDiagnostic.SOURCE_TIMEOUT)
        finally:
            _kill_group(process.pid)  # the unreaped leader keeps the group id reserved
            _reap(process)
        if process.returncode != 0:
            raise HerdrSourceError(HerdrDiagnostic.SOURCE_EXIT_NONZERO)
        return data

    def _read_bounded(process: subprocess.Popen[bytes], deadline: float, max_bytes: int) -> bytes:
        if process.stdout is None:
            raise HerdrSourceError(HerdrDiagnostic.SOURCE_UNAVAILABLE)
        descriptor = process.stdout.fileno()
        chunks: list[bytes] = []
        size = 0
        with selectors.DefaultSelector() as selector:
            selector.register(descriptor, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HerdrSourceError(HerdrDiagnostic.SOURCE_TIMEOUT)
                if not selector.select(remaining):
                    continue
                chunk = os.read(descriptor, _READ_CHUNK)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    return b"".join(chunks)[:max_bytes + 1]

    def _kill_group(pgid: int) -> None:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def _reap(process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=_REAP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()


def _exited_unreaped(pid: int, deadline: float, waitid_primitives: _WaitIdPrimitives | None = None) -> bool:
    """Wait (same deadline) until the leader exits, without reaping it."""
    primitives = waitid_primitives if waitid_primitives is not None else _waitid_primitives()
    if primitives is None:
        raise HerdrSourceError(HerdrDiagnostic.SOURCE_UNAVAILABLE)
    waitid, p_pid, w_exited, w_nohang, w_nowait = primitives
    while True:
        try:
            info = waitid(p_pid, pid, w_exited | w_nohang | w_nowait)
        except ChildProcessError:
            return True
        if info is not None and info.si_pid == pid:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_EXIT_POLL_SECONDS)


class CommandHerdrSource:
    """The live source: ``herdr status --json`` (UNVERIFIED), allowlisted argv only."""

    def __init__(self, args: tuple[str, ...] = HERDR_STATUS_ARGS, *,
                 timeout_seconds: float = COMMAND_TIMEOUT_DEFAULT, max_bytes: int = MAX_RAW_BYTES,
                 which: Callable[[str], str | None] = shutil.which) -> None:
        if tuple(args) not in ALLOWED_COMMAND_ARGS:
            raise HerdrSourceConfigError("HERDR_COMMAND_NOT_ALLOWLISTED")
        if isinstance(timeout_seconds, bool) or not 0.1 <= float(timeout_seconds) <= COMMAND_TIMEOUT_MAX:
            raise HerdrSourceConfigError("HERDR_COMMAND_TIMEOUT_INVALID")
        if isinstance(max_bytes, bool) or not 1 <= max_bytes <= MAX_RAW_BYTES:
            raise HerdrSourceConfigError("HERDR_COMMAND_MAX_BYTES_INVALID")
        self.args = tuple(args)
        self.timeout_seconds = float(timeout_seconds)
        self.max_bytes = max_bytes
        self._which = which

    def installed(self) -> bool:
        if sys.platform == "win32":
            return False  # disabled until bounded streaming exists (CR-06 H03)
        return self._which(HERDR_EXECUTABLE) is not None

    def fetch(self) -> bytes:
        if sys.platform == "win32":
            raise HerdrSourceError(HerdrDiagnostic.COMMAND_SOURCE_UNSUPPORTED_ON_WINDOWS)
        executable = self._which(HERDR_EXECUTABLE)
        if executable is None:
            raise HerdrSourceError(HerdrDiagnostic.NOT_INSTALLED)
        try:
            return bounded_run((executable, *self.args), timeout_seconds=self.timeout_seconds,
                               max_bytes=self.max_bytes)
        except HerdrSourceError:
            raise
        except OSError as exc:
            raise HerdrSourceError(HerdrDiagnostic.SOURCE_UNAVAILABLE) from exc


@dataclass(frozen=True)
class HerdrBinding:
    """Operator-authored association of a source tag with PADOS references."""

    project_ref: str
    work_package_ref: str | None = None

    def __post_init__(self) -> None:
        for value in (self.project_ref, self.work_package_ref):
            if value is not None and (not isinstance(value, str) or not _PADOS_REF.fullmatch(value)):
                raise HerdrSourceConfigError("HERDR_BINDING_REF_INVALID")


def keyed_ref(key: bytes, prefix: str, *parts: str) -> str:
    """``<prefix>-`` + 16 hex of HMAC-SHA256(key, prefix | parts); the raw parts are never kept."""
    message = "\x00".join((prefix, *parts)).encode("utf-8", "surrogatepass")
    return f"{prefix}-{hmac.new(key, message, hashlib.sha256).hexdigest()[:16]}"


def document_digest(key: bytes, raw: bytes) -> str:
    """Keyed digest of a raw document (content-free; used only to detect a repeated document)."""
    return hmac.new(key, b"herdr-document\x00" + bytes(raw), hashlib.sha256).hexdigest()[:32]


@dataclass(frozen=True)
class CollectResult:
    """One collection: telemetry records plus bounded, content-free diagnostics."""

    collected_at: datetime
    outcome: HerdrCollectionOutcome
    source_health: HerdrHealth
    records: tuple[HerdrTelemetryRecord, ...] = ()
    error_code: HerdrDiagnostic | None = None
    source_observed_at: datetime | None = None
    source_time_code: HerdrDiagnostic | None = None
    document_digest: str | None = None
    terminal_content_dropped: int = 0
    authority_claims_ignored: int = 0
    identity_claims_ignored: int = 0
    fields_dropped: int = 0
    entries_refused: int = 0
    refusal_codes: tuple[HerdrDiagnostic, ...] = ()
    schema_id: str = SCHEMA_ID
    semantic_authority: str = SEMANTIC_AUTHORITY_NONE

    def summary(self) -> dict[str, Any]:
        def code(value: HerdrDiagnostic | None) -> str | None:
            return None if value is None else value.value

        return {
            "schema_id": self.schema_id, "collected_at": self.collected_at.isoformat(),
            "outcome": self.outcome.value, "source_health": self.source_health.value,
            "records": len(self.records), "error_code": code(self.error_code),
            "source_observed_at": None if self.source_observed_at is None else self.source_observed_at.isoformat(),
            "source_time_code": code(self.source_time_code),
            "terminal_content_dropped": self.terminal_content_dropped,
            "authority_claims_ignored": self.authority_claims_ignored,
            "identity_claims_ignored": self.identity_claims_ignored, "fields_dropped": self.fields_dropped,
            "entries_refused": self.entries_refused, "refusal_codes": [c.value for c in self.refusal_codes],
            "semantic_authority": self.semantic_authority,
        }


class HerdrDocumentRefused(ValueError):
    def __init__(self, code: HerdrDiagnostic) -> None:
        super().__init__(code.value)
        self.code = code


def is_terminal_content_key(name: str) -> bool:
    normalized = normalized_key(name)
    return any(token in normalized for token in TERMINAL_CONTENT_TOKENS)


@dataclass
class _Counters:
    terminal: int = 0
    authority: int = 0
    identity: int = 0
    dropped: int = 0
    refused: int = 0
    codes: set[HerdrDiagnostic] = field(default_factory=set)

    def refuse(self, code: HerdrDiagnostic) -> None:
        self.refused += 1
        self.codes.add(code)


def _scan(value: Any, counters: _Counters, depth: int) -> None:
    """Count terminal-content, authority and identity keys anywhere in the document."""
    if depth > MAX_RAW_DEPTH:
        raise HerdrDocumentRefused(HerdrDiagnostic.RAW_DOC_TOO_DEEP)
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            if is_terminal_content_key(name):
                counters.terminal += 1
                continue  # never inspected further, never copied
            if is_authority_claim_key(name):
                counters.authority += 1
            elif normalized_key(name) in IDENTITY_CLAIM_KEYS:
                counters.identity += 1
            _scan(item, counters, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _scan(item, counters, depth + 1)


def _first_str(entry: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str):
            return value
    return None


def _raw_id(entry: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    """A bounded raw id (string or integer); used only as HMAC input, never stored."""
    for key in keys:
        value = entry.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if isinstance(value, str):
            return value if 1 <= len(value) <= MAX_RAW_ID_CHARS else None
    return None


def _raw_state(entry: Mapping[str, Any]) -> HerdrAgentState:
    raw = _first_str(entry, _STATE_KEYS)
    if raw is None or len(raw) > MAX_RAW_STATE_CHARS:
        return HerdrAgentState.UNKNOWN
    return RAW_STATE_MAP.get(normalized_key(raw), HerdrAgentState.UNKNOWN)


def _agent_kind(raw: object, counters: _Counters) -> HerdrAgentKind:
    """Closed vocabulary from known source values; labels are never read."""
    if isinstance(raw, dict):
        if "label" in raw or "name" in raw or "title" in raw:
            counters.dropped += 1
        raw = raw.get("kind")
    if raw is None:
        return HerdrAgentKind.UNKNOWN
    if isinstance(raw, str) and len(raw) <= MAX_RAW_STATE_CHARS:
        return AGENT_KIND_MAP.get(normalized_key(raw), HerdrAgentKind.OTHER)
    return HerdrAgentKind.OTHER


def _transition(entry: Mapping[str, Any], collected_at: datetime, counters: _Counters) -> datetime | None:
    raw = _first_str(entry, _TRANSITION_KEYS)
    present = any(key in entry for key in _TRANSITION_KEYS)
    stamp = parse_aware(raw)
    if stamp is None or stamp > collected_at + CLOCK_SKEW:
        if present:
            counters.dropped += 1
        return None
    return stamp


def _health(entry: Mapping[str, Any]) -> HerdrHealth:
    raw = entry.get("health")
    if raw is None:
        return HerdrHealth.HEALTHY
    if isinstance(raw, str) and len(raw) <= MAX_RAW_STATE_CHARS:
        return RAW_HEALTH_MAP.get(normalized_key(raw), HerdrHealth.DEGRADED)
    return HerdrHealth.DEGRADED


def source_time(document: Mapping[str, Any], collected_at: datetime,
                stale_after_seconds: int) -> tuple[datetime | None, HerdrDiagnostic | None]:
    """The validated document-level snapshot time, or ``None`` plus the reason it is unproven."""
    key = next((name for name in SOURCE_TIME_KEYS if name in document), None)
    if key is None:
        return None, HerdrDiagnostic.SOURCE_TIME_MISSING
    stamp = parse_aware(document[key])
    if stamp is None:
        return None, HerdrDiagnostic.SOURCE_TIME_INVALID
    if stamp > collected_at + CLOCK_SKEW:
        return None, HerdrDiagnostic.SOURCE_TIME_IN_FUTURE
    if (collected_at - stamp).total_seconds() > stale_after_seconds:
        return None, HerdrDiagnostic.SOURCE_TIME_TOO_OLD
    return stamp, None


class HerdrAdapter:
    """Raw Herdr status document -> ``pados.herdr.telemetry.v1`` records (telemetry only)."""

    def __init__(self, source: HerdrSource, *, machine_ref: str, ref_key: bytes,
                 bindings: Mapping[str, HerdrBinding] | None = None,
                 stale_after_seconds: int = STALE_AFTER_DEFAULT,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC),
                 max_raw_bytes: int = MAX_RAW_BYTES) -> None:
        if not isinstance(machine_ref, str) or not _MACHINE_REF.fullmatch(machine_ref):
            raise HerdrSourceConfigError("HERDR_MACHINE_REF_INVALID")
        if not isinstance(ref_key, bytes) or len(ref_key) != REF_KEY_BYTES:
            raise HerdrSourceConfigError("HERDR_REF_KEY_INVALID")
        if isinstance(max_raw_bytes, bool) or not 1 <= max_raw_bytes <= MAX_RAW_BYTES:
            raise HerdrSourceConfigError("HERDR_MAX_RAW_BYTES_INVALID")
        if isinstance(stale_after_seconds, bool) or not isinstance(stale_after_seconds, int) \
                or not 1 <= stale_after_seconds <= STALE_AFTER_MAX:
            raise HerdrSourceConfigError("HERDR_STALE_AFTER_SECONDS_INVALID")
        bound = dict(bindings or {})
        if not all(isinstance(tag, str) and isinstance(b, HerdrBinding) for tag, b in bound.items()):
            raise HerdrSourceConfigError("HERDR_BINDINGS_INVALID")
        self.source = source
        self.machine_ref = machine_ref
        self.max_raw_bytes = max_raw_bytes
        self.stale_after_seconds = stale_after_seconds
        self._key = ref_key
        self._bindings = bound
        self._clock = clock

    def collect(self) -> CollectResult:
        """Fetch once from the source and translate. The only polling entry point."""
        collected_at = self._clock()
        try:
            raw = self.source.fetch()
        except HerdrSourceError as exc:
            return self._unreachable(collected_at, diagnostic(exc.code))
        except Exception:  # an unfamiliar source error is never echoed (CR-06 M03)
            return self._unreachable(collected_at, HerdrDiagnostic.SOURCE_ERROR)
        return self.translate(raw, collected_at=collected_at)

    @staticmethod
    def _unreachable(collected_at: datetime, code: HerdrDiagnostic) -> CollectResult:
        return CollectResult(collected_at=collected_at, outcome=HerdrCollectionOutcome.SOURCE_UNREACHABLE,
                             source_health=HerdrHealth.UNREACHABLE, error_code=code)

    def translate(self, raw: bytes, *, collected_at: datetime | None = None) -> CollectResult:
        collected_at = collected_at or self._clock()
        if collected_at.tzinfo is None:
            raise HerdrSourceConfigError("HERDR_CLOCK_NOT_AWARE")
        counters = _Counters()
        try:
            document = self._parse(raw)
            _scan(document, counters, 0)
            observed, time_code = source_time(document, collected_at, self.stale_after_seconds)
            records = self._records(document, collected_at, observed, counters)
        except HerdrDocumentRefused as exc:
            return CollectResult(collected_at=collected_at, outcome=HerdrCollectionOutcome.DOCUMENT_REFUSED,
                                 source_health=HerdrHealth.DEGRADED, error_code=exc.code,
                                 terminal_content_dropped=counters.terminal,
                                 authority_claims_ignored=counters.authority,
                                 identity_claims_ignored=counters.identity)
        partial = counters.refused > 0
        degraded = partial or time_code is not None
        return CollectResult(
            collected_at=collected_at,
            outcome=HerdrCollectionOutcome.COLLECTED_PARTIAL if partial else HerdrCollectionOutcome.COLLECTED,
            source_health=HerdrHealth.DEGRADED if degraded else HerdrHealth.HEALTHY, records=records,
            source_observed_at=observed, source_time_code=time_code,
            document_digest=document_digest(self._key, bytes(raw)),
            terminal_content_dropped=counters.terminal, authority_claims_ignored=counters.authority,
            identity_claims_ignored=counters.identity, fields_dropped=counters.dropped,
            entries_refused=counters.refused,
            refusal_codes=tuple(sorted(counters.codes))[:MAX_REFUSAL_CODES])

    def _parse(self, raw: bytes) -> dict[str, Any]:
        if not isinstance(raw, (bytes, bytearray)):
            raise HerdrDocumentRefused(HerdrDiagnostic.RAW_DOC_NOT_BYTES)
        if len(raw) > self.max_raw_bytes:
            raise HerdrDocumentRefused(HerdrDiagnostic.RAW_DOC_TOO_LARGE)
        try:
            document = json.loads(bytes(raw).decode("utf-8"))
        except (ValueError, RecursionError) as exc:
            raise HerdrDocumentRefused(HerdrDiagnostic.RAW_DOC_INVALID_JSON) from exc
        if not isinstance(document, dict) or not isinstance(document.get("sessions"), list):
            raise HerdrDocumentRefused(HerdrDiagnostic.RAW_DOC_SHAPE_INVALID)
        return document

    def _records(self, document: dict[str, Any], collected_at: datetime, observed: datetime | None,
                 counters: _Counters) -> tuple[HerdrTelemetryRecord, ...]:
        sessions = document["sessions"]
        if len(sessions) > MAX_SESSIONS:
            raise HerdrDocumentRefused(HerdrDiagnostic.RAW_DOC_TOO_MANY_SESSIONS)
        entries: list[tuple[str | None, Mapping[str, Any]]] = []
        for session in sessions:
            if not isinstance(session, dict) or not isinstance(session.get("panes"), list):
                counters.refuse(HerdrDiagnostic.SESSION_SHAPE_INVALID)
                continue
            session_id = _raw_id(session, _SESSION_ID_KEYS)
            for pane in session["panes"]:
                if isinstance(pane, dict):
                    entries.append((session_id, pane))
                else:
                    counters.refuse(HerdrDiagnostic.PANE_SHAPE_INVALID)
                if len(entries) > MAX_ENTRIES:
                    raise HerdrDocumentRefused(HerdrDiagnostic.RAW_DOC_TOO_MANY_PANES)
        records: dict[tuple[str, str, str], HerdrTelemetryRecord] = {}
        for owner, pane in entries:
            record = self._record(owner, pane, collected_at, observed, counters)
            if record is None:
                continue
            if record.key in records:
                counters.refuse(HerdrDiagnostic.PANE_DUPLICATE)
                continue
            records[record.key] = record
        return tuple(records.values())

    def _binding(self, pane: Mapping[str, Any], counters: _Counters) -> HerdrBinding | None:
        tag = _first_str(pane, _TAG_KEYS)
        if tag is None or len(tag) > MAX_TAG_CHARS:
            return None
        binding = self._bindings.get(tag)
        if binding is None:
            counters.dropped += 1  # unbound tag: association omitted
        return binding

    def _record(self, session_id: str | None, pane: Mapping[str, Any], collected_at: datetime,
                observed: datetime | None, counters: _Counters) -> HerdrTelemetryRecord | None:
        pane_id = _raw_id(pane, _PANE_ID_KEYS)
        if session_id is None or pane_id is None:
            counters.refuse(HerdrDiagnostic.REF_INVALID)
            return None
        binding = self._binding(pane, counters)
        try:
            record = HerdrTelemetryRecord(
                machine_ref=self.machine_ref, session_ref=keyed_ref(self._key, "hs", session_id),
                pane_ref=keyed_ref(self._key, "hp", session_id, pane_id),
                agent_kind=_agent_kind(pane.get("agent"), counters), state=_raw_state(pane),
                collected_at=collected_at, source_observed_at=observed,
                last_transition_at=_transition(pane, collected_at, counters), health=_health(pane),
                project_ref=None if binding is None else binding.project_ref,
                work_package_ref=None if binding is None else binding.work_package_ref,
            )
            row = record.to_row()
            assert_record_minimized(row)
            assert_no_authority(row)
        except ValidationError:
            counters.refuse(HerdrDiagnostic.RECORD_INVALID)
            return None
        except DataMinimizationViolation:
            counters.refuse(HerdrDiagnostic.RECORD_NOT_MINIMIZED)
            return None
        except HerdrAuthorityViolation:
            counters.refuse(HerdrDiagnostic.RECORD_CLAIMS_AUTHORITY)
            return None
        return record
