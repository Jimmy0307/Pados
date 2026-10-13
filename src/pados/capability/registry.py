# SPDX-License-Identifier: AGPL-3.0-only
"""Capability registry: installed -> registered, with explicit operator lifecycle transitions.

* install  - an adapter implementation object is placed in the registry (invisible to planning);
* register - an operator registers a validated capability descriptor, or a validated adapter descriptor
  for a capability the operator names (it must match the adapter's own declaration);
* lifecycle - ACTIVE / DEPRECATED / REVOKED are explicit operator calls, recorded as events.

Authorization is NEVER stored here: there is no grant, ACL or "authorized" flag in this module. It is
evaluated per task from the project-bound PADOS grant (``pados.capability.policy``). Adapters never see
the registry; the planner and executor reach implementations only through ``RegisteredAdapter``, which
re-checks that the implementation's descriptor is still the one that was installed and reports the
operator-set lifecycle (so a revocation changes the descriptor digest bound into every decision).

The ONLY invocation path is ``RegisteredAdapter.guarded(final_check, post_check=...)``: on every call it first
runs the adapter-controlled steps (readiness probe, descriptor integrity), THEN the caller's authoritative
``final_check`` (which must not run adapter code), then ``execute``, then the descriptor integrity check and the
caller's ``post_check`` — so a revocation triggered from inside adapter code is always seen by a check, and an
output produced while anything it depended on changed is never returned. Every state change validates and builds its event
(capacity, clock, minimization) BEFORE mutating, then commits state and event together.

Dispatch linearization (CF-03): every mutator (install, register, lifecycle) holds the registry's dispatch
lock (a re-entrant ``threading.RLock``; no thread is ever started here), and the guarded invocation holds the
same lock from the start of ``final_check`` until ``post_check`` has passed. ``post_check`` runs immediately
after the adapter call returns and BEFORE its output is handed to anyone: it re-reads every authority the call
depended on (registry state here, plus the caller's grant, resource, provenance and router seams), and any
change refuses the whole output (``REVOKED_DURING_EXECUTION``). That post-execution check is the fabric's
linearization point: a revocation from ANY authority that completes before it prevents the output's use; one
that completes after it is ordered after the output was taken. A registry change from another thread cannot
complete inside the window at all (it waits for the lock); a running in-process call is never preempted.

Same-thread mutation (callbacks): the lock is re-entrant, so reentrancy alone would let adapter code (its
``available()``, ``descriptor`` or ``execute``) or a seam called by the checks mutate the registry from the
dispatching thread. A per-thread dispatch-in-progress flag (set from the readiness probe until the post-check
ends) refuses every mutator on that thread with ``REGISTRY_MUTATION_DURING_DISPATCH``, and the guarded call
itself is then refused with the same code even if the callback swallowed the error, so no output survives
an attempted mutation. Consequences (documented): mutators on other threads wait for in-flight guarded calls,
and guarded calls on one registry are serialized. Each external seam must return a consistent snapshot of
its own state — that is the operator's responsibility (the fabric cannot lock another authority's store).
"""
from __future__ import annotations

import functools
import logging
import re
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeVar, cast

from pados.capability.adapter import CapabilityAdapter, StageContext, StageOutput, StageRequest
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    LifecycleState,
    descriptor_digest,
)
from pados.lifecycle.minimization import assert_minimized

LOGGER = logging.getLogger("pados.capability.registry")
MAX_CAPABILITIES = 512
MAX_ADAPTERS = 2048
MAX_EVENTS = 10_000
_OPERATOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,79}$")
_REASON = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_DETERMINISM_RANK = {Determinism.DETERMINISTIC: 0, Determinism.SEEDED: 1, Determinism.PROBABILISTIC: 2}
_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.EXPERIMENTAL: frozenset({LifecycleState.ACTIVE, LifecycleState.DEPRECATED, LifecycleState.REVOKED}),
    LifecycleState.ACTIVE: frozenset({LifecycleState.DEPRECATED, LifecycleState.REVOKED}),
    LifecycleState.DEPRECATED: frozenset({LifecycleState.ACTIVE, LifecycleState.REVOKED}),
    LifecycleState.REVOKED: frozenset(),  # terminal
}
REGISTRY_MUTATION_DURING_DISPATCH = "REGISTRY_MUTATION_DURING_DISPATCH"
REVOKED_DURING_EXECUTION = "REVOKED_DURING_EXECUTION"
EVENT_KEYS = frozenset({"sequence", "action", "subject_kind", "subject_id", "from_state", "to_state", "operator",
                        "reason_code", "digest", "at"})


class RegistryError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AdapterIntegrityError(PermissionError):
    """The implementation's descriptor no longer matches what was installed."""


class DispatchRefused(PermissionError):
    """The guarded dispatch itself refused (closed code); any output of the call is withheld."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RegistryAction(StrEnum):
    INSTALLED = "INSTALLED"
    REGISTERED = "REGISTERED"
    LIFECYCLE = "LIFECYCLE"


@dataclass(frozen=True)
class RegistryEvent:
    sequence: int
    action: RegistryAction
    subject_kind: str  # CAPABILITY | ADAPTER
    subject_id: str
    from_state: str | None
    to_state: str | None
    operator: str
    reason_code: str
    digest: str
    at: str


class RegisteredAdapter:
    """Executor-side facade over an installed implementation (never handed to adapter code)."""

    def __init__(self, registry: CapabilityRegistry, adapter_id: str) -> None:
        self._registry = registry
        self.adapter_id = adapter_id

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._registry._effective_adapter(self.adapter_id)

    def available(self) -> bool:
        try:
            self._registry._check_integrity(self.adapter_id)
            return bool(self._registry._implementation(self.adapter_id).available())
        except Exception:  # noqa: BLE001 - a failing readiness probe is "unavailable", never an error path
            return False

    def recorded_descriptor(self) -> AdapterDescriptor:
        """The installed descriptor with the operator lifecycle, read WITHOUT running any adapter code."""
        return self._registry._recorded_adapter(self.adapter_id)

    def guarded(self, final_check: Callable[[bool], None], *,
                post_check: Callable[[bool], None]) -> GuardedAdapter:
        return GuardedAdapter(self, final_check, post_check)


class GuardedAdapter:
    """The single invocation path. ``final_check(ready)`` raises to refuse the call; ``post_check(ready)`` raises
    to withhold its output. Both see the readiness probe result and must not run adapter code."""

    def __init__(self, registered: RegisteredAdapter, final_check: Callable[[bool], None],
                 post_check: Callable[[bool], None]) -> None:
        self._registered = registered
        self._final_check = final_check
        self._post_check = post_check

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._registered.descriptor

    def available(self) -> bool:
        return self._registered.available()

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        registry, adapter_id = self._registered._registry, self._registered.adapter_id
        implementation = registry._implementation(adapter_id)

        def checked(step: Callable[[], _Result]) -> _Result:
            """Run one step; an attempted same-thread registry mutation refuses the dispatch (fail closed)."""
            try:
                result = step()
            except Exception as exc:
                if registry._mutation_attempted():
                    raise DispatchRefused(REGISTRY_MUTATION_DURING_DISPATCH) from exc
                raise
            if registry._mutation_attempted():
                raise DispatchRefused(REGISTRY_MUTATION_DURING_DISPATCH)
            return result

        def unchanged_descriptor() -> None:
            try:
                registry._check_integrity(adapter_id)
            except AdapterIntegrityError as exc:
                raise DispatchRefused(REVOKED_DURING_EXECUTION) from exc

        with registry._dispatching():  # same-thread registry mutations are refused from here on
            ready = checked(self._registered.available)  # adapter code: may try anything
            checked(lambda: registry._check_integrity(adapter_id))  # adapter code (descriptor property)
            with registry._dispatch:  # no registry change from another thread until the post-check passed
                checked(lambda: self._final_check(ready))  # authoritative, after the last pre-call adapter step
                output = checked(lambda: implementation.execute(request, context=context))
                checked(unchanged_descriptor)  # the implementation did not swap its descriptor meanwhile
                checked(lambda: self._post_check(ready))  # linearization point: output withheld on any change
                return output


_Mutator = TypeVar("_Mutator", bound=Callable[..., Any])
_Result = TypeVar("_Result")


def _under_dispatch_lock(method: _Mutator) -> _Mutator:
    """Registry mutators run under the dispatch lock, so they linearize with guarded invocations, and are
    refused outright on a thread that is itself dispatching (a callback re-entering the RLock)."""
    @functools.wraps(method)
    def locked(self: CapabilityRegistry, *args: Any, **kwargs: Any) -> Any:
        self._refuse_during_dispatch()
        with self._dispatch:
            return method(self, *args, **kwargs)

    return cast(_Mutator, locked)


def _operator(value: str) -> str:
    if not isinstance(value, str) or not _OPERATOR.fullmatch(value):
        raise RegistryError("OPERATOR_IDENTITY_INVALID")
    return value


def _reason(value: str) -> str:
    if not isinstance(value, str) or not _REASON.fullmatch(value):
        raise RegistryError("REASON_CODE_INVALID")
    return value


class CapabilityRegistry:
    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._capabilities: dict[str, CapabilityDescriptor] = {}
        self._capability_state: dict[str, LifecycleState] = {}
        self._installed: dict[str, tuple[CapabilityAdapter, AdapterDescriptor, str]] = {}
        self._registered: set[str] = set()
        self._adapter_state: dict[str, LifecycleState] = {}
        self._events: list[RegistryEvent] = []
        self._dispatch = threading.RLock()  # every mutator and every guarded dispatch hold it (CF-03)
        self._in_dispatch = threading.local()  # per-thread dispatch-in-progress flag (not reentrancy)

    # --- registration ------------------------------------------------------------------------

    @_under_dispatch_lock
    def register_capability(self, descriptor: CapabilityDescriptor, *, operator: str,
                            reason_code: str = "OPERATOR_REGISTERED") -> CapabilityDescriptor:
        operator, reason_code = _operator(operator), _reason(reason_code)
        validated = self._revalidated_capability(descriptor)
        if validated.capability_id in self._capabilities:
            raise RegistryError("CAPABILITY_ALREADY_REGISTERED")
        if validated.lifecycle is LifecycleState.REVOKED:
            raise RegistryError("CAPABILITY_REGISTERED_REVOKED")
        if len(self._capabilities) >= MAX_CAPABILITIES:
            raise RegistryError("CAPABILITY_REGISTRY_FULL")
        event = self._event(RegistryAction.REGISTERED, "CAPABILITY", validated.capability_id, None,
                            validated.lifecycle.value, operator, reason_code, descriptor_digest(validated))
        self._capabilities[validated.capability_id] = validated
        self._capability_state[validated.capability_id] = validated.lifecycle
        self._commit(event)
        return validated

    @_under_dispatch_lock
    def install(self, implementation: CapabilityAdapter, *, operator: str) -> str:
        """Place an implementation in the registry. Installed is not registered: it cannot be planned."""
        operator = _operator(operator)
        if not (callable(getattr(implementation, "execute", None))
                and callable(getattr(implementation, "available", None))):
            raise RegistryError("ADAPTER_IMPLEMENTATION_INVALID")
        descriptor = self._read_descriptor(implementation)
        if descriptor.adapter_id in self._installed:
            raise RegistryError("ADAPTER_ALREADY_INSTALLED")
        if len(self._installed) >= MAX_ADAPTERS:
            raise RegistryError("ADAPTER_REGISTRY_FULL")
        digest = descriptor_digest(descriptor)
        event = self._event(RegistryAction.INSTALLED, "ADAPTER", descriptor.adapter_id, None, None, operator,
                            "OPERATOR_INSTALLED", digest)
        self._installed[descriptor.adapter_id] = (implementation, descriptor, digest)
        self._commit(event)
        return descriptor.adapter_id

    @_under_dispatch_lock
    def register_adapter(self, adapter_id: str, *, capability_id: str, operator: str,
                         reason_code: str = "OPERATOR_REGISTERED") -> AdapterDescriptor:
        """Register an installed adapter for the capability the OPERATOR names."""
        operator, reason_code = _operator(operator), _reason(reason_code)
        if adapter_id not in self._installed:
            raise RegistryError("ADAPTER_NOT_INSTALLED")
        if adapter_id in self._registered:
            raise RegistryError("ADAPTER_ALREADY_REGISTERED")
        self._check_integrity(adapter_id)
        _, descriptor, digest = self._installed[adapter_id]
        if descriptor.capability_id != capability_id:
            raise RegistryError("ADAPTER_CAPABILITY_MISMATCH")
        capability = self._capabilities.get(capability_id)
        if capability is None:
            raise RegistryError("CAPABILITY_NOT_REGISTERED")
        if descriptor.lifecycle is LifecycleState.REVOKED:
            raise RegistryError("ADAPTER_REGISTERED_REVOKED")
        if _DETERMINISM_RANK[descriptor.determinism] > _DETERMINISM_RANK[capability.determinism]:
            raise RegistryError("ADAPTER_WEAKENS_CAPABILITY_DETERMINISM")
        if descriptor.delegates_to_worker_router and capability.family is not CapabilityFamily.MODEL_RUNTIME:
            raise RegistryError("ADAPTER_DELEGATION_INVALID")
        event = self._event(RegistryAction.REGISTERED, "ADAPTER", adapter_id, None, descriptor.lifecycle.value,
                            operator, reason_code, digest)
        self._registered.add(adapter_id)
        self._adapter_state[adapter_id] = descriptor.lifecycle
        self._commit(event)
        return descriptor

    # --- explicit operator lifecycle --------------------------------------------------------

    @_under_dispatch_lock
    def set_capability_lifecycle(self, capability_id: str, state: LifecycleState, *, operator: str,
                                 reason_code: str) -> CapabilityDescriptor:
        operator, reason_code = _operator(operator), _reason(reason_code)
        if capability_id not in self._capabilities:
            raise RegistryError("CAPABILITY_NOT_REGISTERED")
        current = self._capability_state[capability_id]
        self._check_transition(current, state)
        descriptor = self._capabilities[capability_id]
        effective = descriptor if state is descriptor.lifecycle else descriptor.model_copy(update={"lifecycle": state})
        event = self._event(RegistryAction.LIFECYCLE, "CAPABILITY", capability_id, current.value, state.value,
                            operator, reason_code, descriptor_digest(effective))
        self._capability_state[capability_id] = state
        self._commit(event)
        return effective

    @_under_dispatch_lock
    def set_adapter_lifecycle(self, adapter_id: str, state: LifecycleState, *, operator: str,
                              reason_code: str) -> AdapterDescriptor:
        operator, reason_code = _operator(operator), _reason(reason_code)
        if adapter_id not in self._registered:
            raise RegistryError("ADAPTER_NOT_REGISTERED")
        current = self._adapter_state[adapter_id]
        self._check_transition(current, state)
        _, descriptor, _ = self._installed[adapter_id]
        effective = descriptor if state is descriptor.lifecycle else descriptor.model_copy(update={"lifecycle": state})
        event = self._event(RegistryAction.LIFECYCLE, "ADAPTER", adapter_id, current.value, state.value, operator,
                            reason_code, descriptor_digest(effective))
        self._adapter_state[adapter_id] = state
        self._commit(event)
        return effective

    # --- read side (planner / executor) ------------------------------------------------------

    def capability(self, capability_id: str) -> CapabilityDescriptor:
        descriptor = self._capabilities.get(capability_id)
        if descriptor is None:
            raise RegistryError("CAPABILITY_NOT_REGISTERED")
        state = self._capability_state[capability_id]
        return descriptor if state is descriptor.lifecycle else descriptor.model_copy(update={"lifecycle": state})

    def adapters_for(self, capability_id: str) -> tuple[RegisteredAdapter, ...]:
        """Registered adapters of one capability, in deterministic adapter_id order."""
        ids = sorted(aid for aid in self._registered if self._installed[aid][1].capability_id == capability_id)
        return tuple(RegisteredAdapter(self, aid) for aid in ids)

    def adapter(self, adapter_id: str) -> RegisteredAdapter:
        if adapter_id not in self._registered:
            raise RegistryError("ADAPTER_NOT_REGISTERED")
        return RegisteredAdapter(self, adapter_id)

    def is_installed(self, adapter_id: str) -> bool:
        return adapter_id in self._installed

    def is_registered(self, adapter_id: str) -> bool:
        return adapter_id in self._registered

    @property
    def events(self) -> tuple[RegistryEvent, ...]:
        return tuple(self._events)

    # --- internals ---------------------------------------------------------------------------

    @contextmanager
    def _dispatching(self) -> Iterator[None]:
        """Mark this thread as dispatching (nesting-safe); mutators on this thread are refused meanwhile."""
        state = self._in_dispatch
        depth = getattr(state, "depth", 0)
        if depth == 0:
            state.attempted = False
        state.depth = depth + 1
        try:
            yield
        finally:
            state.depth = depth

    def _refuse_during_dispatch(self) -> None:
        state = self._in_dispatch
        if getattr(state, "depth", 0) > 0:
            state.attempted = True  # remembered: the guarded call is refused even if the error is swallowed
            raise RegistryError(REGISTRY_MUTATION_DURING_DISPATCH)

    def _mutation_attempted(self) -> bool:
        return bool(getattr(self._in_dispatch, "attempted", False))

    def _effective_adapter(self, adapter_id: str) -> AdapterDescriptor:
        if adapter_id not in self._registered:
            raise RegistryError("ADAPTER_NOT_REGISTERED")
        self._check_integrity(adapter_id)
        return self._recorded_adapter(adapter_id)

    def _recorded_adapter(self, adapter_id: str) -> AdapterDescriptor:
        if adapter_id not in self._registered:
            raise RegistryError("ADAPTER_NOT_REGISTERED")
        descriptor = self._installed[adapter_id][1]
        state = self._adapter_state[adapter_id]
        return descriptor if state is descriptor.lifecycle else descriptor.model_copy(update={"lifecycle": state})

    def _implementation(self, adapter_id: str) -> CapabilityAdapter:
        return self._installed[adapter_id][0]

    def _check_integrity(self, adapter_id: str) -> None:
        implementation, _, digest = self._installed[adapter_id]
        try:
            live = self._read_descriptor(implementation)
        except RegistryError as exc:
            raise AdapterIntegrityError("ADAPTER_DESCRIPTOR_CHANGED_SINCE_INSTALL") from exc
        if descriptor_digest(live) != digest:
            raise AdapterIntegrityError("ADAPTER_DESCRIPTOR_CHANGED_SINCE_INSTALL")

    @staticmethod
    def _read_descriptor(implementation: CapabilityAdapter) -> AdapterDescriptor:
        try:
            descriptor = implementation.descriptor
            if not isinstance(descriptor, AdapterDescriptor):
                raise TypeError("not an AdapterDescriptor")
            return AdapterDescriptor.model_validate(descriptor.model_dump(mode="json"))
        except Exception as exc:  # noqa: BLE001 - any malformed/forged descriptor is refused
            raise RegistryError("ADAPTER_DESCRIPTOR_INVALID") from exc

    @staticmethod
    def _revalidated_capability(descriptor: CapabilityDescriptor) -> CapabilityDescriptor:
        if not isinstance(descriptor, CapabilityDescriptor):
            raise RegistryError("CAPABILITY_DESCRIPTOR_INVALID")
        try:  # a model_construct()-forged descriptor cannot skip validation
            return CapabilityDescriptor.model_validate(descriptor.model_dump(mode="json"))
        except ValueError as exc:
            raise RegistryError("CAPABILITY_DESCRIPTOR_INVALID") from exc

    @staticmethod
    def _check_transition(current: LifecycleState, target: LifecycleState) -> None:
        if target not in _TRANSITIONS[current]:
            raise RegistryError("LIFECYCLE_TRANSITION_NOT_ALLOWED")

    def _event(self, action: RegistryAction, subject_kind: str, subject_id: str, from_state: str | None,
               to_state: str | None, operator: str, reason_code: str, digest: str) -> RegistryEvent:
        """Build and validate the event for a change that has NOT happened yet (nothing is mutated here)."""
        if len(self._events) >= MAX_EVENTS:
            raise RegistryError("REGISTRY_EVENT_LOG_FULL")
        try:
            at = self._clock().isoformat()
        except Exception as exc:  # noqa: BLE001 - no trustworthy timestamp means no state change
            raise RegistryError("REGISTRY_CLOCK_UNAVAILABLE") from exc
        event = RegistryEvent(len(self._events) + 1, action, subject_kind, subject_id, from_state, to_state,
                              operator, reason_code, digest, at)
        assert_minimized(_event_record(event), allowed_keys=EVENT_KEYS)
        return event

    def _commit(self, event: RegistryEvent) -> None:
        """Append the pre-validated event right after its state change (no failure path in between)."""
        self._events.append(event)
        try:
            LOGGER.info("capability_registry_event", extra={"registry_event": _event_record(event)})
        except Exception:  # noqa: BLE001,S110 - a log sink failure must not split state from its event
            pass


def _event_record(event: RegistryEvent) -> dict[str, object]:
    return {**asdict(event), "action": event.action.value}
