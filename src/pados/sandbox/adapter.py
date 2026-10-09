# SPDX-License-Identifier: AGPL-3.0-only
"""Sandbox adapter interface and a deterministic simulated adapter.

Lifecycle: CREATE -> STAGE -> EXECUTE -> CAPTURE -> VERIFY -> DESTROY. The
simulated adapter runs no code: it replays a declared, deterministic step
function over staged bytes so the governance path (policy, exchange, capture,
hashing, teardown) is testable in CI. A live VM adapter implements the same
protocol and is validated in hypervisor UAT. Input and output artifact names are
plain file names only: what the sandboxed step returns is untrusted and can never
address a path outside the per-run inbox/outbox.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pados.sandbox.policy import (
    DataEgressPolicy,
    PolicyError,
    SandboxRequest,
    profile_policy,
    validate_request,
)


# Plain file name: no separators, drive or stream colon, leading dot/dash/space, or device name.
_PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.()+-]{0,199}")
_WINDOWS_DEVICES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"{p}{n}" for p in ("COM", "LPT") for n in range(10))})


def _plain_name(name: object, code: str) -> str:
    """Accept a name only if Windows and POSIX resolve it to the same, ordinary file.

    Windows strips trailing dots and spaces (``CON `` is the console device, ``report.`` is
    ``report``) and treats a reserved device stem as the device whatever the extension.
    """
    if not isinstance(name, str) or not _PLAIN_NAME.fullmatch(name) or ".." in name or name.endswith((".", " ")):
        raise PolicyError(code)
    if name.split(".", 1)[0].rstrip(" ").upper() in _WINDOWS_DEVICES:
        raise PolicyError(code)
    return name


def _claim_name(seen: set[str], name: str, code: str) -> None:
    """Names that Windows would treat as the same file (case-insensitive) may not coexist."""
    key = name.casefold()
    if key in seen:
        raise PolicyError(code)
    seen.add(key)


class Phase(StrEnum):
    CREATED = "CREATED"
    STAGED = "STAGED"
    EXECUTED = "EXECUTED"
    CAPTURED = "CAPTURED"
    VERIFIED = "VERIFIED"
    DESTROYED = "DESTROYED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Artifact:
    name: str
    sha256: str
    size_bytes: int


@dataclass
class SandboxRun:
    run_id: str
    request: SandboxRequest
    phases: list[str] = field(default_factory=list)
    artifacts: tuple[Artifact, ...] = ()
    semantic_authority: str = "NONE"
    failure: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "profile": self.request.profile.value,
            "isolation": self.request.isolation.value,
            "phases": list(self.phases),
            "artifacts": [a.__dict__ for a in self.artifacts],
            "semantic_authority": self.semantic_authority,
            "failure": self.failure,
            "network_default": profile_policy(self.request.profile).default_action.value,
            "internet_egress": profile_policy(self.request.profile).internet_egress,
        }


class SandboxAdapter(Protocol):
    adapter_kind: str

    def run(
        self, request: SandboxRequest, inputs: Mapping[str, bytes], egress: DataEgressPolicy
    ) -> SandboxRun: ...


# Deterministic stand-in for "the work": maps staged inputs to output artifacts.
StepFunction = Callable[[Mapping[str, bytes]], Mapping[str, bytes]]


class SimulatedSandboxAdapter:
    adapter_kind = "SIMULATED_DETERMINISTIC"

    def __init__(self, step: StepFunction, *, base_dir: Path | None = None) -> None:
        self._step = step
        self._base = base_dir

    def run(
        self, request: SandboxRequest, inputs: Mapping[str, bytes], egress: DataEgressPolicy
    ) -> SandboxRun:
        run = SandboxRun(run_id=f"sbx-{uuid.uuid4().hex[:16]}", request=request)
        workspace = Path(tempfile.mkdtemp(prefix="pados-sbx-", dir=self._base))
        try:
            validate_request(request)
            run.phases.append(Phase.CREATED.value)
            inbox = workspace / "in"
            outbox = workspace / "out"
            inbox.mkdir()
            outbox.mkdir()
            names: set[str] = set()
            for name, data in inputs.items():
                _claim_name(names, _plain_name(name, "INPUT_NAME_INVALID"), "INPUT_NAME_COLLISION")
                (inbox / name).write_bytes(data)
            run.phases.append(Phase.STAGED.value)
            staged = {p.name: p.read_bytes() for p in inbox.iterdir()}
            outputs = self._step(staged)
            run.phases.append(Phase.EXECUTED.value)
            captured: list[Artifact] = []
            for name, data in sorted(outputs.items()):
                _claim_name(names, _plain_name(name, "ARTIFACT_NAME_INVALID"), "ARTIFACT_NAME_COLLISION")
                egress.admit_artifact(name, len(data))
                (outbox / name).write_bytes(data)
                captured.append(Artifact(name, hashlib.sha256(data).hexdigest(), len(data)))
            run.phases.append(Phase.CAPTURED.value)
            for artifact in captured:
                data = (outbox / artifact.name).read_bytes()
                if hashlib.sha256(data).hexdigest() != artifact.sha256:
                    raise PolicyError("ARTIFACT_HASH_MISMATCH")
            run.artifacts = tuple(captured)
            run.phases.append(Phase.VERIFIED.value)
        except PolicyError as exc:
            run.failure = exc.code
            run.phases.append(Phase.FAILED.value)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
            run.phases.append(Phase.DESTROYED.value)
        return run
