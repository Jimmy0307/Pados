# SPDX-License-Identifier: AGPL-3.0-only
"""Dominance-based Rough Set Approach (DRSA) with DOMLEM-style certain-rule induction.

Public method definitions implemented here (no third-party code):

* Greco, Matarazzo, Slowinski, "Rough sets theory for multicriteria decision analysis", EJOR 129 (2001):
  dominance relation, P-dominating cone D+(x) = {y : y D_P x}, P-dominated cone D-(x) = {y : x D_P y},
  upward unions Cl_t>= and downward unions Cl_t<=, lower approximations {x : D+(x) subset Cl_t>=} and
  {x : D-(x) subset Cl_t<=}, upper approximations (union of cones of the members), boundaries, accuracy
  |lower| / |upper| and the quality of approximation |U - all boundaries| / |U|.
* Greco, Matarazzo, Slowinski, Stefanowski, "An algorithm for induction of decision rules consistent
  with the dominance principle" (RSCTC 2000, LNAI 2005): DOMLEM sequential covering of each lower
  approximation, elementary conditions taken from the not-yet-covered positive objects, best condition by
  consistency |[E] n B| / |[E]| then coverage |[E] n G|, removal of redundant conditions and redundant
  rules, and only minimal rules kept across unions (strongest union first).

Regular attributes (criterion NONE) take part in dominance by equality. Deterministic tie-break (an
implementation choice, documented): lower criterion position first, then the more general threshold.
Objects are bitmasks over the dataset order so every set operation is exact and bounded.

Work is bounded by a deterministic operation budget (``operation_budget``), counted in elementary steps
from the first step on: table construction (one per cell read, preference computed and decision class
looked up), threshold-mask construction, one per cone/approximation membership test, union-membership
test, candidate-condition evaluation, threshold collected, coverage-check conjunct, rule-redundancy union,
generalization CONDITION comparison and reported object/value lookup - never by wall-clock time. The
budget and ``max_rules`` are enforced INSIDE induction and redundancy elimination: the run aborts at the
first step that would exceed either (``DRSA_OPERATION_BUDGET_EXCEEDED`` / ``DRSA_RULE_LIMIT_EXCEEDED``).
``max_rules`` bounds the rules held at any moment (accepted rules plus the working rules of the union
being covered), which also bounds the output. An optional wall-clock ``Deadline`` (set by the adapter or
the replay verifier, never a parameter) rides on the meter and can only abort a run
(``ALGORITHM_DEADLINE_EXCEEDED``); it never changes a completed result.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from pados.packs.algorithms.contracts import (
    AlgorithmError,
    Criterion,
    Dataset,
    FeatureType,
    complete_rows,
    ordered_ids,
)
from pados.packs.algorithms.meter import Deadline, WorkMeter

ALGORITHM_ID = "drsa.domlem"
ALGORITHM_VERSION = "3"  # v3: table/mask/output work metered, generalization charged per condition
MAX_OBJECTS = 1000
MAX_CRITERIA = 32
MAX_CLASSES = 32
DEFAULT_OPERATION_BUDGET = 20_000_000
MAX_OPERATION_BUDGET = 100_000_000


class OperationBudget(WorkMeter):
    """The DRSA work meter: ``spend`` refuses BEFORE the limit would be exceeded (``used <= limit``)."""

    def __init__(self, limit: int, *, deadline: Deadline | None = None) -> None:
        super().__init__(limit, maximum=MAX_OPERATION_BUDGET, invalid_code="DRSA_OPERATION_BUDGET_INVALID",
                         exceeded_code="DRSA_OPERATION_BUDGET_EXCEEDED", deadline=deadline)


class DrsaParameters(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    criteria: tuple[str, ...] | None = Field(default=None, max_length=MAX_CRITERIA)  # default: all conditions
    max_rules: int = Field(default=500, ge=1, le=5000)
    operation_budget: int = Field(default=DEFAULT_OPERATION_BUDGET, ge=1, le=MAX_OPERATION_BUDGET)
    include_covered_objects: bool = True
    precision: int = Field(default=10, ge=1, le=15)

    @field_validator("criteria")
    @classmethod
    def unique(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and (not value or len(set(value)) != len(value)):
            raise ValueError("DRSA_CRITERIA_INVALID")
        return value


class RuleCondition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    attribute: str
    operator: str = Field(pattern=r"^(>=|<=|=)$")
    value: float | str


class RuleConclusion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    union: str = Field(pattern=r"^(AT_LEAST|AT_MOST)$")
    decision_class: str


class Rule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    conditions: tuple[RuleCondition, ...] = Field(min_length=1)
    conclusion: RuleConclusion
    rule_type: str = Field(pattern=r"^CERTAIN$")
    support: int = Field(ge=1)
    strength: float
    covered_objects: tuple[str, ...] | None = None


class UnionApproximation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    union: str = Field(pattern=r"^(AT_LEAST|AT_MOST)$")
    decision_class: str
    members: tuple[str, ...]
    lower: tuple[str, ...]
    upper: tuple[str, ...]
    boundary: tuple[str, ...]
    accuracy: float


class DrsaResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    criteria: tuple[str, ...]
    decision: str
    classes: tuple[str, ...]
    objects: int
    dropped_objects: int
    approximations: tuple[UnionApproximation, ...]
    quality_of_approximation: float
    inconsistent_objects: tuple[str, ...]
    rules: tuple[Rule, ...]


@dataclass(frozen=True)
class _Attribute:
    name: str
    regular: bool  # criterion NONE: equality only
    cost: bool  # COST criterion: preference = -value, so displayed operators flip
    prefs: tuple[float, ...]  # per object; higher = better for criteria
    raw: tuple[float | str, ...]


@dataclass
class _Table:
    ids: tuple[str, ...]
    attributes: tuple[_Attribute, ...]
    classes: tuple[str, ...]
    decision: tuple[int, ...]  # class index per object (higher = better)
    full: int
    budget: OperationBudget
    _cache: dict[tuple[int, str], dict[float, int]]

    def mask(self, attribute: int, op: str, pref: float) -> int:
        """Objects satisfying ``pref_attribute op pref``; thresholds always come from observed values."""
        masks = self._cache.get((attribute, op))
        if masks is None:
            masks = self._build(attribute, op)
            self._cache[(attribute, op)] = masks
        bits = masks.get(pref)
        if bits is None:
            raise AlgorithmError("DRSA_THRESHOLD_UNKNOWN")
        return bits

    def _build(self, attribute: int, op: str) -> dict[float, int]:
        self.budget.spend(len(self.ids))  # one step per object read (cumulative masks: one more per value)
        exact: dict[float, int] = {}
        for index, value in enumerate(self.attributes[attribute].prefs):
            exact[value] = exact.get(value, 0) | 1 << index
        if op == "=":
            return exact
        self.budget.spend(len(exact))
        cumulative: dict[float, int] = {}
        bits = 0
        for value in sorted(exact, reverse=op == ">="):  # >=: best first; <=: worst first
            bits |= exact[value]
            cumulative[value] = bits
        return cumulative


def _preference(column_type: FeatureType, criterion: Criterion, domain: tuple[str, ...], cell: float | str) -> float:
    if column_type is FeatureType.NUMERIC:
        value = float(cell)
    else:
        value = float(domain.index(str(cell)))
    return -value if criterion is Criterion.COST else value


def _table(dataset: Dataset, parameters: DrsaParameters, budget: OperationBudget) -> tuple[_Table, int]:
    """Validated table; every pass over the rows is metered before it runs."""
    if dataset.decision is None:
        raise AlgorithmError("DRSA_DECISION_REQUIRED")
    decision_index = dataset.column_index(dataset.decision)
    decision_column = dataset.columns[decision_index]
    if decision_column.type is not FeatureType.ORDINAL or decision_column.criterion is Criterion.NONE:
        raise AlgorithmError("DRSA_DECISION_NOT_ORDERED")
    if len(decision_column.domain) < 2 or len(decision_column.domain) > MAX_CLASSES:
        raise AlgorithmError("DRSA_DECISION_CLASSES_INVALID")
    names = parameters.criteria or tuple(c.name for c in dataset.columns if c.name != dataset.decision)
    if not names:
        raise AlgorithmError("DRSA_NO_CRITERIA")
    if dataset.decision in names:
        raise AlgorithmError("DRSA_CRITERIA_INVALID")
    if len(names) > MAX_CRITERIA:
        raise AlgorithmError("DRSA_TOO_MANY_CRITERIA")
    positions = sorted(dataset.column_index(name) for name in names)  # dataset order, deterministic
    budget.spend(len(dataset.rows) * len(dataset.columns))  # missing-value policy reads every cell
    ids, rows, dropped = complete_rows(dataset)
    if len(ids) > MAX_OBJECTS:
        raise AlgorithmError("DRSA_TOO_MANY_OBJECTS")
    attributes = []
    for position in positions:
        column = dataset.columns[position]
        budget.spend(len(rows) * (1 if column.type is FeatureType.NUMERIC else max(1, len(column.domain))))
        raw = tuple(row[position] for row in rows)
        prefs = tuple(_preference(column.type, column.criterion, column.domain, cell) for cell in raw)
        attributes.append(_Attribute(column.name, column.criterion is Criterion.NONE,
                                     column.criterion is Criterion.COST, prefs, raw))
    # Classes ordered worst -> best.
    classes = decision_column.domain if decision_column.criterion is Criterion.GAIN else decision_column.domain[::-1]
    budget.spend(len(rows) * len(classes))
    decision = tuple(classes.index(str(row[decision_index])) for row in rows)
    table = _Table(ids, tuple(attributes), tuple(classes), decision, (1 << len(ids)) - 1, budget, {})
    return table, dropped


def _cones(table: _Table, budget: OperationBudget) -> tuple[list[int], list[int]]:
    """D+(x) (objects dominating x) and D-(x) (objects dominated by x) for every object x."""
    dominating: list[int] = []
    dominated: list[int] = []
    for index in range(len(table.ids)):
        budget.spend(len(table.attributes))
        up = down = table.full
        for position, attribute in enumerate(table.attributes):
            pref = attribute.prefs[index]
            if attribute.regular:
                same = table.mask(position, "=", pref)
                up &= same
                down &= same
            else:
                up &= table.mask(position, ">=", pref)
                down &= table.mask(position, "<=", pref)
        dominating.append(up)
        dominated.append(down)
    return dominating, dominated


def _members(table: _Table, t: int, upward: bool) -> int:
    table.budget.spend(len(table.decision))
    bits = 0
    for index, cls in enumerate(table.decision):
        if (upward and cls >= t) or (not upward and cls <= t):
            bits |= 1 << index
    return bits


def _subset(a: int, b: int) -> bool:
    return a & ~b == 0


@dataclass(frozen=True)
class _Candidate:
    conditions: dict[int, float]  # attribute position -> preference threshold
    cover: int


def _condition_op(attribute: _Attribute, upward: bool) -> str:
    if attribute.regular:
        return "="
    return ">=" if upward else "<="


def _cover(table: _Table, conditions: dict[int, float], upward: bool, budget: OperationBudget) -> int:
    budget.spend(len(conditions))
    bits = table.full
    for position, pref in conditions.items():
        bits &= table.mask(position, _condition_op(table.attributes[position], upward), pref)
    return bits


def _indices(mask: int) -> list[int]:
    found: list[int] = []
    while mask:
        low = mask & -mask
        found.append(low.bit_length() - 1)
        mask ^= low
    return found


def _better(a: tuple[int, int, int, int, float, int, float, int],
            b: tuple[int, int, int, int, float, int, float, int]) -> bool:
    """Higher consistency (exact), then coverage of uncovered objects, then the deterministic tie-break."""
    left, right = a[0] * b[1], b[0] * a[1]
    if left != right:
        return left > right
    return (a[2], a[3], a[4]) > (b[2], b[3], b[4])


def _find_rules(table: _Table, positive: int, upward: bool, held: int, max_rules: int,
                budget: OperationBudget) -> list[_Candidate]:
    """DOMLEM covering of one lower approximation ``positive`` (B). ``held`` rules are already accepted;
    the working set may never push the total past ``max_rules``."""
    rules: list[_Candidate] = []
    uncovered = positive  # G
    covered = 0
    while uncovered:
        if held + len(rules) >= max_rules:
            raise AlgorithmError("DRSA_RULE_LIMIT_EXCEEDED")  # another rule is needed and none may be held
        conditions: dict[int, float] = {}
        cover = table.full
        candidates_from = uncovered  # S
        while not conditions or not _subset(cover, positive):
            # best = (consistent, size, coverage, -position, generality, position, pref, narrowed); consistency
            # positive/size is compared exactly by cross-multiplication.
            best: tuple[int, int, int, int, float, int, float, int] | None = None
            members = _indices(candidates_from)
            budget.spend(len(members))
            for position, attribute in enumerate(table.attributes):
                op = _condition_op(attribute, upward)
                budget.spend(len(members))
                for pref in sorted({attribute.prefs[index] for index in members}):
                    budget.spend()
                    narrowed = cover & table.mask(position, op, pref)
                    if conditions and narrowed == cover:
                        continue  # no-op condition: never chosen, guarantees progress
                    generality = -pref if op in (">=", "=") else pref
                    candidate = ((narrowed & positive).bit_count(), narrowed.bit_count(),
                                 (narrowed & uncovered).bit_count(), -position, generality, position, pref, narrowed)
                    if best is None or _better(candidate, best):
                        best = candidate
            if best is None:  # unreachable for a true lower approximation; fail closed anyway
                raise AlgorithmError("DRSA_RULE_INDUCTION_STALLED")
            position, pref, narrowed = best[5], best[6], best[7]
            conditions[position] = pref
            cover = narrowed
            candidates_from &= table.mask(position, _condition_op(table.attributes[position], upward), pref)
        for position in list(conditions):  # drop redundant elementary conditions
            if len(conditions) == 1:
                break
            trial = {key: value for key, value in conditions.items() if key != position}
            trial_cover = _cover(table, trial, upward, budget)
            if _subset(trial_cover, positive):
                conditions = trial
                cover = trial_cover
        rules.append(_Candidate(dict(sorted(conditions.items())), cover))
        covered |= cover  # earlier rules never change, so the union is incremental
        uncovered = positive & ~covered
    for rule in list(rules):  # drop rules whose objects the others already cover
        budget.spend(len(rules))
        others = 0
        for other in rules:
            if other is not rule:
                others |= other.cover
        if _subset(positive, others):
            rules.remove(rule)
    return rules


def _generalizes(general: _Candidate, specific: _Candidate, table: _Table, upward: bool,
                 budget: OperationBudget) -> bool:
    """True when every condition of ``general`` is implied by ``specific`` (syntactic); one step per
    condition compared."""
    for position, pref in general.conditions.items():
        budget.spend()
        if position not in specific.conditions:
            return False
        other = specific.conditions[position]
        if table.attributes[position].regular:
            if other != pref:
                return False
        elif (upward and other < pref) or (not upward and other > pref):
            return False
    return True


def _rule(table: _Table, candidate: _Candidate, cls: int, upward: bool,
          parameters: DrsaParameters) -> dict[str, Any]:
    count = len(table.ids)
    table.budget.spend(count * len(candidate.conditions) + (count if parameters.include_covered_objects else 0))
    conditions = []
    for position, pref in candidate.conditions.items():
        attribute = table.attributes[position]
        op = _condition_op(attribute, upward)
        if attribute.cost and op != "=":
            op = "<=" if op == ">=" else ">="
        conditions.append({"attribute": attribute.name, "operator": op,
                           "value": attribute.raw[attribute.prefs.index(pref)]})
    support = candidate.cover.bit_count()
    return {
        "conditions": conditions,
        "conclusion": {"union": "AT_LEAST" if upward else "AT_MOST", "decision_class": table.classes[cls]},
        "rule_type": "CERTAIN",
        "support": support,
        "strength": _ratio(Fraction(support, len(table.ids)), parameters.precision),
        "covered_objects": ordered_ids(table.ids, candidate.cover) if parameters.include_covered_objects else None,
    }


def _ratio(value: Fraction, digits: int) -> float:
    return float(round(value, digits)) + 0.0


def compute(dataset: Dataset, parameters: DrsaParameters, *, budget: OperationBudget | None = None,
            deadline: Deadline | None = None) -> tuple[dict[str, Any], dict[str, float]]:
    """DRSA approximations + DOMLEM certain rules. Returns (result, metrics).

    ``budget`` defaults to ``OperationBudget(parameters.operation_budget, deadline=deadline)``; a caller may
    pass its own meter with the same limit (and the same deadline) to observe ``used`` (also reported as
    the ``operations`` metric)."""
    if budget is None:
        budget = OperationBudget(parameters.operation_budget, deadline=deadline)
    elif budget.limit != parameters.operation_budget or (deadline is not None and budget.deadline is not deadline):
        raise AlgorithmError("DRSA_OPERATION_BUDGET_INVALID")
    table, dropped = _table(dataset, parameters, budget)
    dominating, dominated = _cones(table, budget)
    count = len(table.ids)
    approximations: list[dict[str, Any]] = []
    lowers: list[tuple[int, bool, int]] = []
    boundaries = 0
    unions = [(t, True) for t in range(1, len(table.classes))]
    unions += [(t, False) for t in range(len(table.classes) - 1)]
    for t, upward in unions:
        members = _members(table, t, upward)
        cones = dominating if upward else dominated
        lower = upper = 0
        budget.spend(count)
        for index in range(count):
            if _subset(cones[index], members):
                lower |= 1 << index
            if members >> index & 1:
                upper |= cones[index]
        boundary = upper & ~lower
        boundaries |= boundary
        budget.spend(4 * count)  # the four reported object lists
        accuracy = Fraction(lower.bit_count(), upper.bit_count()) if upper else Fraction(1)
        approximations.append({
            "union": "AT_LEAST" if upward else "AT_MOST", "decision_class": table.classes[t],
            "members": ordered_ids(table.ids, members), "lower": ordered_ids(table.ids, lower),
            "upper": ordered_ids(table.ids, upper), "boundary": ordered_ids(table.ids, boundary),
            "accuracy": _ratio(accuracy, parameters.precision),
        })
        lowers.append((t, upward, lower))
    rules: list[dict[str, Any]] = []
    for upward in (True, False):
        # Strongest union first: Cl_n>= ... Cl_2>= and Cl_1<= ... Cl_(n-1)<=.
        ordered = sorted((item for item in lowers if item[1] is upward), key=lambda item: item[0],
                         reverse=upward)
        accepted: list[_Candidate] = []
        for t, _, lower in ordered:
            for candidate in _find_rules(table, lower, upward, len(rules), parameters.max_rules, budget):
                budget.spend()
                if any(_generalizes(existing, candidate, table, upward, budget) for existing in accepted):
                    continue  # a rule with weaker conditions and a stronger conclusion exists
                accepted.append(candidate)
                rules.append(_rule(table, candidate, t, upward, parameters))
                if len(rules) > parameters.max_rules:
                    raise AlgorithmError("DRSA_RULE_LIMIT_EXCEEDED")
    quality = Fraction(count - boundaries.bit_count(), count)
    budget.spend(count)  # the reported inconsistent objects
    result = {
        "criteria": [attribute.name for attribute in table.attributes],
        "decision": str(dataset.decision),
        "classes": list(table.classes),
        "objects": count,
        "dropped_objects": dropped,
        "approximations": approximations,
        "quality_of_approximation": _ratio(quality, parameters.precision),
        "inconsistent_objects": ordered_ids(table.ids, boundaries),
        "rules": rules,
    }
    metrics = {"quality_of_approximation": _ratio(quality, parameters.precision), "rule_count": float(len(rules)),
               "objects": float(count), "dropped_objects": float(dropped),
               "inconsistent_objects": float(boundaries.bit_count()), "operations": float(budget.used)}
    return result, metrics
