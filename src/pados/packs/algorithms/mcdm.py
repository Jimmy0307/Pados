# SPDX-License-Identifier: AGPL-3.0-only
"""Multi-criteria decision (MCDM) contracts and the DEMATEL reference implementation.

Generic contract: every MCDM method consumes an ``algorithms.dataset`` artifact plus method parameters and
yields a result envelope whose ``result.method`` names the method. Methods are separate capabilities
(``MULTI_CRITERIA_DECISION_<METHOD>``) so a grant names exactly the method it authorizes and no fallback
can swap one method's semantics for another's.

DEMATEL (Gabus & Fontela, Battelle Geneva 1972/1973; normalisation and threshold as described in Tzeng,
Chiang & Li, Expert Systems with Applications 32 (2007) and Ou Yang et al. 2008):
  s = max(max row sum, max column sum) of the direct-relation matrix A;  N = A / s;
  T = N (I - N)^-1 computed EXACTLY with rational Gauss-Jordan elimination (a singular I - N is an
  explicit ``DEMATEL_MATRIX_SINGULAR`` error, never a pseudo-inverse or a silent approximation);
  D = row sums of T, R = column sums of T, prominence D + R, relation D - R (cause group: D - R > 0);
  impact-relation map edges t_ij > threshold (default: the mean of T).

Bounded exact arithmetic: every input value (and an explicit threshold) must be a decimal with at most
``DECIMAL_PLACES`` (9) places - a value with more places, a subnormal float or an extreme exponent is refused
with a closed code (no silent rounding) - so every entry is an integer multiple of 10^-9 no larger than
10^6. Every rational operation - each product, quotient and difference of Gauss-Jordan elimination, and
each product and PARTIAL SUM of the matrix product, the normalisation and the score sums - is one step of
a shared ``ArithmeticBudget`` (parameter ``operation_budget``; ``DEMATEL_OPERATION_BUDGET_EXCEEDED``) and
its numerator and denominator are checked against ``MAX_FRACTION_BITS`` right after that single step
(``DEMATEL_ARITHMETIC_BUDGET_EXCEEDED``), never only after a whole row or dot product. An optional
wall-clock ``Deadline`` rides on the budget and can only abort (``ALGORITHM_DEADLINE_EXCEEDED``).

DANP, Choquet integral and ROV (range of value) are contract-only here: parameter/result schemas and
descriptors exist, their adapters report ``available() == False`` and never execute.
"""
from __future__ import annotations

import math
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pados.packs.algorithms.contracts import AlgorithmError, Dataset, square_matrix
from pados.packs.algorithms.meter import Deadline, WorkMeter

DEMATEL_ID = "mcdm.dematel"
DEMATEL_VERSION = "2"  # v2: shared arithmetic-operation budget, per-step bit checks, "operations" metric
MAX_FACTORS = 32
MAX_DIRECT_VALUE = 1_000_000.0
DECIMAL_PLACES = 9
MAX_FRACTION_BITS = 8192  # numerator/denominator bit length cap for every intermediate Fraction
DEFAULT_OPERATION_BUDGET = 1_000_000  # 32 factors need about 0.2 M rational operations
MAX_OPERATION_BUDGET = 10_000_000
_QUANTUM = 10**DECIMAL_PLACES


class ArithmeticBudget(WorkMeter):
    """One step per rational operation of a DEMATEL run, shared by every phase (``used <= limit``)."""

    def __init__(self, limit: int = DEFAULT_OPERATION_BUDGET, *, deadline: Deadline | None = None) -> None:
        super().__init__(limit, maximum=MAX_OPERATION_BUDGET, invalid_code="DEMATEL_OPERATION_BUDGET_INVALID",
                         exceeded_code="DEMATEL_OPERATION_BUDGET_EXCEEDED", deadline=deadline)


class McdmMethod(StrEnum):
    DEMATEL = "DEMATEL"
    DANP = "DANP"
    CHOQUET = "CHOQUET"
    ROV = "ROV"


class ThresholdMode(StrEnum):
    MEAN = "MEAN"
    VALUE = "VALUE"


class DematelParameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    threshold_mode: ThresholdMode = ThresholdMode.MEAN
    threshold_value: float | None = Field(default=None, ge=0.0, le=1e6)
    precision: int = Field(default=10, ge=1, le=15)
    operation_budget: int = Field(default=DEFAULT_OPERATION_BUDGET, ge=1, le=MAX_OPERATION_BUDGET)

    @model_validator(mode="after")
    def threshold_consistent(self) -> DematelParameters:
        if (self.threshold_mode is ThresholdMode.VALUE) != (self.threshold_value is not None):
            raise ValueError("DEMATEL_THRESHOLD_INVALID")
        if self.threshold_value is not None and _decimal_or_none(self.threshold_value) is None:
            raise ValueError("DEMATEL_THRESHOLD_INVALID")
        return self


class FactorScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: str
    dispatch: float  # D (row sum of T)
    receive: float  # R (column sum of T)
    prominence: float  # D + R
    relation: float  # D - R
    group: str = Field(pattern=r"^(CAUSE|EFFECT|NEUTRAL)$")


class Edge(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    target: str
    weight: float


class DematelResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    method: str = Field(pattern=r"^DEMATEL$")
    factors: tuple[str, ...]
    normalization: float
    normalized: tuple[tuple[float, ...], ...]
    total_relation: tuple[tuple[float, ...], ...]
    threshold: float
    scores: tuple[FactorScore, ...]
    edges: tuple[Edge, ...]


# --- contract-only methods: parameter and result schemas -----------------------------------------


class DanpParameters(BaseModel):
    """DEMATEL-based ANP: clusters of criteria and the DEMATEL settings used for the weighted supermatrix."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    clusters: dict[str, tuple[str, ...]] = Field(min_length=1, max_length=16)
    dematel: DematelParameters = DematelParameters()
    limit_power_max: int = Field(default=200, ge=1, le=1000)
    epsilon: float = Field(default=1e-9, ge=1e-15, le=1e-2)


class DanpResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    method: str = Field(pattern=r"^DANP$")
    criteria: tuple[str, ...]
    influential_weights: dict[str, float]
    limit_power: int


class ChoquetParameters(BaseModel):
    """Discrete Choquet integral: a capacity (fuzzy measure) on criterion subsets, keyed by '+'-joined names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criteria: tuple[str, ...] = Field(min_length=1, max_length=12)
    capacity: dict[str, float] = Field(min_length=1, max_length=4096)
    check_monotone: bool = True


class ChoquetResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    method: str = Field(pattern=r"^CHOQUET$")
    scores: dict[str, float]
    ranking: tuple[str, ...]


class RovParameters(BaseModel):
    """Range of value (Yakowitz, Lane & Szidarovszky 1993): weight-order constraints, normalised utilities."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    criteria_order: tuple[str, ...] = Field(min_length=1, max_length=32)  # most to least important


class RovResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    method: str = Field(pattern=r"^ROV$")
    utility_ranges: dict[str, tuple[float, float]]
    midpoint_ranking: tuple[str, ...]
    dominated: tuple[str, ...]


# --- DEMATEL --------------------------------------------------------------------------------------


Matrix = list[list[Fraction]]


def _decimal_or_none(value: float) -> Fraction | None:
    """The decimal the user wrote (shortest repr), if it is finite, normal (or zero) and has at most
    ``DECIMAL_PLACES`` places; ``None`` otherwise. A float repr has at most 17 significant digits and a
    3-digit exponent, so even an extreme exponent costs one small Fraction before it is refused."""
    if not math.isfinite(value) or (value != 0.0 and abs(value) < sys.float_info.min):
        return None
    exact = Fraction(repr(value))
    return exact if _QUANTUM % exact.denominator == 0 else None


def _exact(value: float) -> Fraction:
    if value != 0.0 and abs(value) < sys.float_info.min:
        raise AlgorithmError("DEMATEL_VALUE_SUBNORMAL")
    exact = _decimal_or_none(value)
    if exact is None:
        raise AlgorithmError("DEMATEL_VALUE_PRECISION_EXCEEDED")
    return exact


def _checked(value: Fraction, max_bits: int) -> Fraction:
    if value.numerator.bit_length() > max_bits or value.denominator.bit_length() > max_bits:
        raise AlgorithmError("DEMATEL_ARITHMETIC_BUDGET_EXCEEDED")
    return value


class _Arithmetic:
    """Metered, bit-checked rational operations: one budget step and one bit-length check per operation.

    The step (and the deadline) is reserved BEFORE an operation is performed, so an exhausted budget or an
    expired deadline never allows one more rational operation; the result's size is checked afterwards."""

    def __init__(self, budget: ArithmeticBudget, max_bits: int) -> None:
        self.budget = budget
        self.max_bits = max_bits

    def check(self, value: Fraction) -> Fraction:
        self.budget.spend()
        return _checked(value, self.max_bits)

    def mul(self, a: Fraction, b: Fraction) -> Fraction:
        self.budget.spend()
        return _checked(a * b, self.max_bits)

    def div(self, a: Fraction, b: Fraction) -> Fraction:
        self.budget.spend()
        return _checked(a / b, self.max_bits)

    def sub(self, a: Fraction, b: Fraction) -> Fraction:
        self.budget.spend()
        return _checked(a - b, self.max_bits)

    def add(self, a: Fraction, b: Fraction) -> Fraction:
        self.budget.spend()
        return _checked(a + b, self.max_bits)

    def total(self, values: Iterable[Fraction]) -> Fraction:
        """Sum in a fixed order; every partial sum is checked."""
        acc = Fraction(0)
        for value in values:
            acc = self.add(acc, value)
        return acc

    def dot(self, left: Sequence[Fraction], right: Iterable[Fraction]) -> Fraction:
        """Every product and every partial sum is checked as soon as it exists."""
        acc = Fraction(0)
        for a, b in zip(left, right, strict=True):
            acc = self.add(acc, self.mul(a, b))
        return acc


def _arithmetic(budget: ArithmeticBudget | None, max_bits: int) -> _Arithmetic:
    return _Arithmetic(ArithmeticBudget() if budget is None else budget, max_bits)


def invert(matrix: Matrix, *, max_bits: int = MAX_FRACTION_BITS, budget: ArithmeticBudget | None = None) -> Matrix:
    """Exact Gauss-Jordan inverse over the rationals; raises DEMATEL_MATRIX_SINGULAR, and
    DEMATEL_ARITHMETIC_BUDGET_EXCEEDED as soon as any single intermediate exceeds ``max_bits``."""
    ar = _arithmetic(budget, max_bits)
    size = len(matrix)
    work = [[ar.check(value) for value in row[:] + [Fraction(int(i == j)) for j in range(size)]]
            for i, row in enumerate(matrix)]
    for column in range(size):
        pivot = next((r for r in range(column, size) if work[r][column] != 0), None)
        if pivot is None:
            raise AlgorithmError("DEMATEL_MATRIX_SINGULAR")
        work[column], work[pivot] = work[pivot], work[column]
        lead = work[column][column]
        work[column] = [ar.div(value, lead) for value in work[column]]
        for r in range(size):
            if r != column and work[r][column] != 0:
                factor = work[r][column]
                work[r] = [ar.sub(a, ar.mul(factor, b)) for a, b in zip(work[r], work[column], strict=True)]
    return [row[size:] for row in work]


def multiply(left: Matrix, right: Matrix, *, max_bits: int = MAX_FRACTION_BITS,
             budget: ArithmeticBudget | None = None) -> Matrix:
    """Exact product; every product and partial sum is metered and bit-checked as it is formed."""
    ar = _arithmetic(budget, max_bits)
    size = len(right[0])
    return [[ar.dot(row, (right[k][j] for k in range(len(right)))) for j in range(size)] for row in left]


@dataclass(frozen=True)
class DematelExact:
    factors: tuple[str, ...]
    scale: Fraction
    normalized: Matrix
    total: Matrix


def dematel_exact(factors: tuple[str, ...], direct: tuple[tuple[float, ...], ...], *,
                  budget: ArithmeticBudget | None = None) -> DematelExact:
    """Normalisation and T = N (I - N)^-1, all on one shared ``budget`` (default: the default limit)."""
    size = len(factors)
    if size < 2:
        raise AlgorithmError("DEMATEL_TOO_FEW_FACTORS")
    if any(direct[i][i] != 0.0 for i in range(size)):
        raise AlgorithmError("DEMATEL_DIAGONAL_NOT_ZERO")
    budget = ArithmeticBudget() if budget is None else budget
    ar = _Arithmetic(budget, MAX_FRACTION_BITS)
    a = [[ar.check(_exact(value)) for value in row] for row in direct]
    scale = max(max(ar.total(row) for row in a), max(ar.total(a[i][j] for i in range(size)) for j in range(size)))
    if scale == 0:
        raise AlgorithmError("DEMATEL_ZERO_MATRIX")
    n = [[ar.div(value, scale) for value in row] for row in a]
    identity_minus_n = [[ar.sub(Fraction(int(i == j)), n[i][j]) for j in range(size)] for i in range(size)]
    total = multiply(n, invert(identity_minus_n, budget=budget), budget=budget)
    return DematelExact(factors, scale, n, total)


def _f(value: Fraction, digits: int) -> float:
    return float(round(value, digits)) + 0.0


def compute_dematel(dataset: Dataset, parameters: DematelParameters, *,
                    deadline: Deadline | None = None) -> tuple[dict[str, Any], dict[str, float]]:
    factors, direct = square_matrix(dataset, low=0.0, high=MAX_DIRECT_VALUE, max_size=MAX_FACTORS, code="DEMATEL")
    budget = ArithmeticBudget(parameters.operation_budget, deadline=deadline)
    exact = dematel_exact(factors, direct, budget=budget)
    ar = _Arithmetic(budget, MAX_FRACTION_BITS)
    size = len(factors)
    t = exact.total
    dispatch = [ar.total(t[i]) for i in range(size)]
    receive = [ar.total(t[i][j] for i in range(size)) for j in range(size)]
    threshold = (ar.div(ar.total(dispatch), Fraction(size * size))
                 if parameters.threshold_mode is ThresholdMode.MEAN else _exact(float(parameters.threshold_value or 0)))
    digits = parameters.precision
    scores = []
    for i, name in enumerate(factors):
        relation = ar.sub(dispatch[i], receive[i])
        prominence = ar.add(dispatch[i], receive[i])
        group = "CAUSE" if relation > 0 else ("EFFECT" if relation < 0 else "NEUTRAL")
        scores.append({"factor": name, "dispatch": _f(dispatch[i], digits), "receive": _f(receive[i], digits),
                       "prominence": _f(prominence, digits), "relation": _f(relation, digits),
                       "group": group})
    edges = [{"source": factors[i], "target": factors[j], "weight": _f(t[i][j], digits)}
             for i in range(size) for j in range(size) if t[i][j] > threshold]
    result = {
        "method": McdmMethod.DEMATEL.value,
        "factors": list(factors),
        "normalization": _f(exact.scale, digits),
        "normalized": [[_f(v, digits) for v in row] for row in exact.normalized],
        "total_relation": [[_f(v, digits) for v in row] for row in t],
        "threshold": _f(threshold, digits),
        "scores": scores,
        "edges": edges,
    }
    metrics = {"factors": float(size), "edges": float(len(edges)), "threshold": _f(threshold, digits),
               "cause_factors": float(sum(1 for s in scores if s["group"] == "CAUSE")),
               "operations": float(budget.used)}
    return result, metrics
