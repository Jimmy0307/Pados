"""SEAM-01: OutcomeAdvisor honours the extended ScoreAdvisor seam exactly as the Capability Fabric planner calls it.

The planner passes the classification as its string value, the candidates to ``context_hash`` as sorted
``(adapter_id, adapter_version)`` pairs, and an advice-independent ``plan_binding_hash``. Advice must apply only
when all three match, and must never widen feasibility.
"""
from __future__ import annotations

from pados.capability.seams import ScoreAdvisor, apply_advice
from pados.enterprise.classification import DataClassification
from pados.learning.advisor import UNBOUND_CONTEXT_HASH, as_classification
from tests.learning.learning_support import CONF, SCANNED_ZH, sha
from tests.learning.test_cr11_learning import TASK, A, B, C, enabled

PAIRS = tuple(sorted((d.adapter_id, d.version) for d in (A, B)))
FEASIBLE = ["ocr.engine-a", "ocr.engine-b"]


def _planner_call(seam: ScoreAdvisor, binding: str, classification: str = CONF.value,
                  pairs: tuple[tuple[str, str], ...] = PAIRS) -> tuple[str, dict[str, float]]:
    expected = seam.context_hash(**TASK, context_features=SCANNED_ZH, candidates=pairs,
                                 classification=classification, plan_binding_hash=binding)
    advice = seam.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=classification,
                         plan_binding_hash=binding)
    return expected, apply_advice(FEASIBLE, advice, context_hash=expected)


def test_planner_calling_convention_applies_bound_advice() -> None:
    seam: ScoreAdvisor = enabled()
    expected, adjustments = _planner_call(seam, sha("plan-1"))
    assert expected != UNBOUND_CONTEXT_HASH
    assert set(adjustments) == set(FEASIBLE) and all(abs(v) <= 0.25 for v in adjustments.values())


def test_advice_for_another_plan_binding_or_classification_never_applies() -> None:
    seam: ScoreAdvisor = enabled()
    advice = seam.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF.value,
                         plan_binding_hash=sha("plan-1"))
    other_plan = seam.context_hash(**TASK, context_features=SCANNED_ZH, candidates=PAIRS,
                                   classification=CONF.value, plan_binding_hash=sha("plan-2"))
    other_class = seam.context_hash(**TASK, context_features=SCANNED_ZH, candidates=PAIRS,
                                    classification=DataClassification.RESTRICTED.value,
                                    plan_binding_hash=sha("plan-1"))
    wider = seam.context_hash(**TASK, context_features=SCANNED_ZH,
                              candidates=tuple(sorted((d.adapter_id, d.version) for d in (A, B, C))),
                              classification=CONF.value, plan_binding_hash=sha("plan-1"))
    for wrong in (other_plan, other_class, wider):
        assert apply_advice([*FEASIBLE, "ocr.engine-c"], advice, context_hash=wrong) == {}


def test_unknown_classification_value_is_unbound_and_fails_closed() -> None:
    seam: ScoreAdvisor = enabled()
    expected, adjustments = _planner_call(seam, sha("plan-1"), classification="NOT_A_CLASSIFICATION")
    assert expected == UNBOUND_CONTEXT_HASH and adjustments == {}
    assert as_classification("NOT_A_CLASSIFICATION") is None and as_classification(3) is None
    assert as_classification(CONF.value) is CONF


def test_advice_never_widens_feasibility_through_the_seam() -> None:
    seam: ScoreAdvisor = enabled()
    expected, _ = _planner_call(seam, sha("plan-1"))
    advice = seam.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF.value,
                         plan_binding_hash=sha("plan-1"))
    assert set(apply_advice(["ocr.engine-a"], advice, context_hash=expected)) == {"ocr.engine-a"}


def test_fabric_task_type_form_is_accepted_and_digested() -> None:
    """The fabric's TaskContract task types are lowercase/dotted; the advisor binds them (never CONTEXT_INVALID)."""
    from pados.learning.records import task_ref_for

    assert task_ref_for("document_decision_analysis").startswith("task-")
    assert task_ref_for("document_decision_analysis") != task_ref_for("DOCUMENT_DECISION_ANALYSIS")
    for bad in ("Document", "x", "has space", "document/../x", "a" * 70):
        try:
            task_ref_for(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    seam: ScoreAdvisor = enabled()
    bound = seam.context_hash(task_type="document_decision_analysis", capability_id="DOCUMENT_OCR",
                              context_features={}, candidates=PAIRS, classification=CONF.value,
                              plan_binding_hash=sha("plan-1"))
    assert bound != UNBOUND_CONTEXT_HASH
