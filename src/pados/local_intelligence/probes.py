# SPDX-License-Identifier: AGPL-3.0-only
"""Structured probe execution and probe evidence (no shell, bounded output, explicit outcomes).

A probe is one fixed observation (a command with a constant argv, a file read, an OS API call or a loopback HTTP
GET). Its stdout is evidence to be validated by the caller, never trusted authority. ``ProbeSession`` keeps the
raw bytes so an evidence capture can archive them next to their SHA-256 recorded in the profile.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_PROBE_OUTPUT_BYTES = 256 * 1024
DEFAULT_PROBE_TIMEOUT_SECONDS = 10.0
PROBE_ID_PATTERN = r"^[a-z][a-z0-9_.-]{1,63}$"
_PROBE_ID = re.compile(PROBE_ID_PATTERN)
_DISPLAY_UNSAFE = re.compile(r"[\x00-\x1f\x7f<>`$\\{}|;]")


class Basis(StrEnum):
    """Where a fact comes from. Facts of different bases are never merged into one value."""

    UPSTREAM_FACT = "UPSTREAM_FACT"
    MODEL_CARD_CLAIM = "MODEL_CARD_CLAIM"
    COMMUNITY_CLAIM = "COMMUNITY_CLAIM"
    PADOS_MEASURED = "PADOS_MEASURED"
    UNKNOWN = "UNKNOWN"


class ProbeOutcome(StrEnum):
    OK = "OK"
    NOT_FOUND = "NOT_FOUND"  # the tool/endpoint/file does not exist on this host
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    TIMED_OUT = "TIMED_OUT"
    FAILED = "FAILED"  # ran, but reported failure (non-zero exit, HTTP error, OS error)
    INVALID_OUTPUT = "INVALID_OUTPUT"  # ran, but the output did not validate


class ProbeRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str
    outcome: ProbeOutcome
    code: str | None = Field(default=None, max_length=120)
    duration_ms: float = Field(ge=0)
    output_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    output_bytes: int | None = Field(default=None, ge=0)

    @field_validator("probe_id")
    @classmethod
    def probe_id_pattern(cls, value: str) -> str:
        if not _PROBE_ID.fullmatch(value):
            raise ValueError("PROBE_ID_INVALID")
        return value


def display_text(value: object, *, max_length: int = 120) -> str | None:
    """Bounded single-line display text from untrusted tool output, or None when it is unusable."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > max_length or _DISPLAY_UNSAFE.search(text):
        return None
    return text


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    truncated: bool = False


class CommandNotFound(Exception):
    pass


class CommandTimedOut(Exception):
    pass


CommandRunner = Callable[[Sequence[str], float], CommandResult]


def run_command(argv: Sequence[str], timeout: float) -> CommandResult:
    """Run a constant argv without a shell. The executable is resolved on PATH; stderr is discarded."""
    if not argv or any(not isinstance(part, str) or "\x00" in part for part in argv):
        raise ValueError("PROBE_ARGV_INVALID")
    executable = shutil.which(argv[0])
    if executable is None:
        raise CommandNotFound(argv[0])
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, shell=False, resolved executable
            [executable, *argv[1:]], shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise CommandTimedOut(argv[0]) from None
    except OSError:
        raise CommandNotFound(argv[0]) from None
    stdout = completed.stdout or b""
    return CommandResult(completed.returncode, stdout[:MAX_PROBE_OUTPUT_BYTES],
                         truncated=len(stdout) > MAX_PROBE_OUTPUT_BYTES)


@dataclass
class ProbeSession:
    """Collects probe records (and the raw outputs they were derived from) for one measurement."""

    runner: CommandRunner = run_command
    clock: Callable[[], float] = time.monotonic
    records: list[ProbeRecord] = field(default_factory=list)
    raw: dict[str, bytes] = field(default_factory=dict)

    def record(self, probe_id: str, outcome: ProbeOutcome, started: float, *, code: str | None = None,
               output: bytes | None = None) -> ProbeRecord:
        record = ProbeRecord(
            probe_id=probe_id, outcome=outcome, code=code,
            duration_ms=round(max(0.0, self.clock() - started) * 1000, 3),
            output_sha256=hashlib.sha256(output).hexdigest() if output is not None else None,
            output_bytes=len(output) if output is not None else None)
        self.records.append(record)
        if output is not None:
            self.raw[probe_id] = output
        return record

    def command(self, probe_id: str, argv: Sequence[str], *,
                timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS) -> bytes | None:
        """Run one command probe; returns stdout on exit 0, otherwise records why and returns None."""
        started = self.clock()
        try:
            result = self.runner(argv, timeout)
        except CommandNotFound:
            self.record(probe_id, ProbeOutcome.NOT_FOUND, started, code="COMMAND_NOT_FOUND")
            return None
        except CommandTimedOut:
            self.record(probe_id, ProbeOutcome.TIMED_OUT, started, code="COMMAND_TIMED_OUT")
            return None
        if result.truncated:
            self.record(probe_id, ProbeOutcome.INVALID_OUTPUT, started, code="OUTPUT_TOO_LARGE", output=result.stdout)
            return None
        if result.returncode != 0:
            self.record(probe_id, ProbeOutcome.FAILED, started, code=f"EXIT_{result.returncode}", output=result.stdout)
            return None
        self.record(probe_id, ProbeOutcome.OK, started, output=result.stdout)
        return result.stdout

    def invalid(self, probe_id: str, code: str) -> None:
        """Downgrade the latest OK record of ``probe_id`` when its output fails validation."""
        for index in range(len(self.records) - 1, -1, -1):
            record = self.records[index]
            if record.probe_id == probe_id:
                self.records[index] = record.model_copy(update={"outcome": ProbeOutcome.INVALID_OUTPUT, "code": code})
                return
