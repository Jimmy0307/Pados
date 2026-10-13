# SPDX-License-Identifier: AGPL-3.0-only
"""Fuzzy Cognitive Map (FCM) inference (generic; pure Python).

Public method definitions implemented here (no third-party code):

* Kosko, "Fuzzy cognitive maps", Int. J. Man-Machine Studies 24 (1986):
  A_i(t+1) = f( sum_{j != i} A_j(t) * w_ji ).
* Modified Kosko rule (Stylios & Groumpos, 2004): A_i(t+1) = f( A_i(t) + sum_{j != i} A_j(t) * w_ji ).
* Rescaled rule (Papageorgiou, 2011): A_i(t+1) = f( (2A_i(t) - 1) + sum_{j != i} (2A_j(t) - 1) * w_ji ).
* Transfer functions: sigmoid 1 / (1 + e^(-lambda x)), hyperbolic tangent tanh(lambda x), bivalent
  (1 if x > 0 else 0) and trivalent (sign of x).

The weight matrix is a square dataset: row i, column j holds w_ij in [-1, 1], the influence of concept i on
concept j; self-loops are refused. Inference stops at a fixed point (max |A(t+1) - A(t)| <= epsilon), at a
limit cycle (A(t+1) within epsilon of an earlier state, period <= max_period), or at max_iterations
(status MAX_ITERATIONS: reported, not hidden). Clamped concepts keep their EXACT initial value - the
decimal the user wrote - throughout the computation, so their influence on other concepts uses that value
(v3; v2 quantized it first). Only their REPORTED representation (final state, trajectory) is rounded to
``precision`` like every other reported value.

Replay determinism: the platform ``libm`` is never used. Inputs (floats) enter as the decimal their
shortest repr denotes; every step is computed with the standard-library ``decimal`` module in a fixed
local context (``WORK_DIGITS`` significant digits, ROUND_HALF_EVEN), whose exp and division are correctly
rounded by specification (General Decimal Arithmetic / IEEE 754-2008), and the new state is QUANTIZED
half-even to ``precision`` decimals before it is used for the next step, the epsilon test or cycle
detection (clamped concepts are constant and exact, so they never affect those decisions). The trajectory
is therefore decided on deterministic decimals that do not depend on the host floating-point library.
Reported floats are the quantized decimals. An optional wall-clock ``Deadline`` is polled once per
iteration and can only abort (``ALGORITHM_DEADLINE_EXCEEDED``).
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pados.packs.algorithms.contracts import AlgorithmError, Dataset, quantize, square_matrix
from pados.packs.algorithms.meter import Deadline

ALGORITHM_ID = "fcm.inference"
ALGORITHM_VERSION = "3"  # v3: clamped concepts keep their exact initial decimal (v2: decimal, quantized steps)
MAX_CONCEPTS = 64
WORK_DIGITS = 40
_ONE = Decimal(1)
_TWO = Decimal(2)


class TransferFunction(StrEnum):
    SIGMOID = "SIGMOID"
    TANH = "TANH"
    BIVALENT = "BIVALENT"
    TRIVALENT = "TRIVALENT"


class UpdateRule(StrEnum):
    KOSKO = "KOSKO"
    MODIFIED_KOSKO = "MODIFIED_KOSKO"
    RESCALED = "RESCALED"


class FcmStatus(StrEnum):
    FIXED_POINT = "FIXED_POINT"
    LIMIT_CYCLE = "LIMIT_CYCLE"
    MAX_ITERATIONS = "MAX_ITERATIONS"


class FcmParameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    initial_state: dict[str, float] = Field(max_length=MAX_CONCEPTS)
    transfer: TransferFunction = TransferFunction.SIGMOID
    steepness: float = Field(default=1.0, gt=0.0, le=100.0)  # lambda
    update_rule: UpdateRule = UpdateRule.KOSKO
    epsilon: float = Field(default=1e-6, ge=1e-12, le=0.5)
    max_iterations: int = Field(default=100, ge=1, le=500)
    max_period: int = Field(default=16, ge=2, le=64)
    clamped: tuple[str, ...] = Field(default=(), max_length=MAX_CONCEPTS)
    record_trajectory: bool = True
    precision: int = Field(default=10, ge=1, le=15)

    @field_validator("initial_state")
    @classmethod
    def finite(cls, value: dict[str, float]) -> dict[str, float]:
        if any(isinstance(v, bool) or not math.isfinite(v) for v in value.values()):
            raise ValueError("FCM_INITIAL_STATE_INVALID")
        return value


class FcmResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    concepts: tuple[str, ...]
    status: FcmStatus
    iterations: int = Field(ge=0)
    period: int | None = None
    final_state: dict[str, float]
    trajectory: tuple[tuple[float, ...], ...] | None = None


State = tuple[Decimal, ...]


def decimal_context() -> Context:
    """Fixed arithmetic context; huge exponents stay finite (exp(-6400) underflows to a tiny decimal)."""
    return Context(prec=WORK_DIGITS, rounding=ROUND_HALF_EVEN, Emax=999_999, Emin=-999_999,
                   traps=[InvalidOperation, DivisionByZero, Overflow])


def decimal_of(value: float) -> Decimal:
    """The decimal a finite float's shortest repr denotes (what the user wrote, not its binary expansion)."""
    if not math.isfinite(value):
        raise AlgorithmError("FCM_VALUE_NOT_FINITE")
    return Decimal(repr(float(value))) + 0  # + 0 under the context: -0 -> 0, rounded to WORK_DIGITS


def _sigmoid(x: Decimal) -> Decimal:
    if x >= 0:
        return _ONE / (_ONE + (-x).exp())
    e = x.exp()
    return e / (_ONE + e)


def _tanh(x: Decimal) -> Decimal:
    if x >= 0:
        e = (-_TWO * x).exp()
        return (_ONE - e) / (_ONE + e)
    e = (_TWO * x).exp()
    return (e - _ONE) / (e + _ONE)


def _transfer(kind: TransferFunction, steepness: Decimal, x: Decimal) -> Decimal:
    if kind is TransferFunction.SIGMOID:
        return _sigmoid(steepness * x)
    if kind is TransferFunction.TANH:
        return _tanh(steepness * x)
    if kind is TransferFunction.BIVALENT:
        return _ONE if x > 0 else Decimal(0)
    return _ONE if x > 0 else (-_ONE if x < 0 else Decimal(0))


def quantize_state(value: Decimal, digits: int) -> Decimal:
    """Half-even to ``digits`` decimals; -0 is normalised to 0."""
    return value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_EVEN) + 0


def _activation_range(kind: TransferFunction) -> tuple[float, float]:
    return (0.0, 1.0) if kind in {TransferFunction.SIGMOID, TransferFunction.BIVALENT} else (-1.0, 1.0)


def step(state: State, weights: tuple[State, ...], parameters: FcmParameters, clamped: frozenset[int]) -> State:
    """One synchronous update of every concept, quantized to ``parameters.precision`` decimals; a clamped
    concept keeps its current (exact) value unchanged.

    Must run inside ``decimal_context()`` (``compute`` does); the sum is accumulated in a fixed order."""
    size = len(state)
    steepness = decimal_of(parameters.steepness)
    rescaled = parameters.update_rule is UpdateRule.RESCALED
    source = tuple(_TWO * a - _ONE for a in state) if rescaled else state
    nxt: list[Decimal] = []
    for i in range(size):
        if i in clamped:
            nxt.append(state[i])
            continue
        total = Decimal(0)
        for j in range(size):
            if j != i:
                total += source[j] * weights[j][i]
        if parameters.update_rule is not UpdateRule.KOSKO:
            total += source[i]
        nxt.append(quantize_state(_transfer(parameters.transfer, steepness, total), parameters.precision))
    return tuple(nxt)


def _reported(value: Decimal, digits: int) -> float:
    """The reported representation: half-even on the DECIMAL (an exact clamped 0.15 reports 0.2 at
    precision 1, not the 0.1 that rounding the float 0.1499... would give)."""
    return quantize(float(quantize_state(value, digits)), digits)


def _distance(a: State, b: State) -> Decimal:
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


def initial_state(concepts: tuple[str, ...], parameters: FcmParameters, clamped: frozenset[int]) -> State:
    """Clamped concepts: the exact decimal the user wrote (never rounded for computation); every other
    concept is quantized, so every compared free value lives on the same decimal grid.

    Must run inside ``decimal_context()``."""
    return tuple(decimal_of(parameters.initial_state[name]) if i in clamped
                 else quantize_state(decimal_of(parameters.initial_state[name]), parameters.precision)
                 for i, name in enumerate(concepts))


def compute(dataset: Dataset, parameters: FcmParameters, *,
            deadline: Deadline | None = None) -> tuple[dict[str, Any], dict[str, float]]:
    concepts, weights = square_matrix(dataset, low=-1.0, high=1.0, max_size=MAX_CONCEPTS, code="FCM")
    if any(weights[i][i] != 0.0 for i in range(len(concepts))):
        raise AlgorithmError("FCM_SELF_LOOP_NOT_ALLOWED")
    if set(parameters.initial_state) != set(concepts):
        raise AlgorithmError("FCM_INITIAL_STATE_CONCEPTS_MISMATCH")
    if not set(parameters.clamped) <= set(concepts) or len(set(parameters.clamped)) != len(parameters.clamped):
        raise AlgorithmError("FCM_CLAMPED_UNKNOWN")
    low, high = _activation_range(parameters.transfer)
    if any(not low <= parameters.initial_state[name] <= high for name in concepts):
        raise AlgorithmError("FCM_INITIAL_STATE_OUT_OF_RANGE")
    clamped = frozenset(concepts.index(name) for name in parameters.clamped)
    digits = parameters.precision
    status, period = FcmStatus.MAX_ITERATIONS, None
    iterations = 0
    try:
        with localcontext(decimal_context()):
            matrix = tuple(tuple(decimal_of(w) for w in row) for row in weights)
            epsilon = decimal_of(parameters.epsilon)
            history: list[State] = [initial_state(concepts, parameters, clamped)]
            for iterations in range(1, parameters.max_iterations + 1):
                if deadline is not None:
                    deadline.check()
                nxt = step(history[-1], matrix, parameters, clamped)
                history.append(nxt)
                if _distance(nxt, history[-2]) <= epsilon:
                    status, period = FcmStatus.FIXED_POINT, 1
                    break
                earliest = max(0, len(history) - 1 - parameters.max_period)
                match = next((k for k in range(len(history) - 3, earliest - 1, -1)
                              if _distance(nxt, history[k]) <= epsilon), None)
                if match is not None:
                    status, period = FcmStatus.LIMIT_CYCLE, len(history) - 1 - match
                    break
    except ArithmeticError:
        failed = True
    else:
        failed = False
    if failed:
        raise AlgorithmError("FCM_ARITHMETIC_ERROR")
    with localcontext(decimal_context()):
        final = [_reported(v, digits) for v in history[-1]]
        trajectory = ([[_reported(v, digits) for v in state] for state in history]
                      if parameters.record_trajectory else None)  # converted only when it is reported
    result = {
        "concepts": list(concepts),
        "status": status.value,
        "iterations": iterations,
        "period": period,
        "final_state": dict(zip(concepts, final, strict=True)),
        "trajectory": trajectory,
    }
    metrics = {"iterations": float(iterations), "converged": 1.0 if status is FcmStatus.FIXED_POINT else 0.0,
               "period": float(period or 0), "concepts": float(len(concepts))}
    return result, metrics
