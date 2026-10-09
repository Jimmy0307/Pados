"""CF-01 seam: ``OutcomeAdvisor.issued`` reports exactly the advice it issued and durably logged per plan binding."""
from __future__ import annotations

import json
from pathlib import Path

from pados.capability.seams import ScoreAdvisor, apply_advice
from pados.learning.advisor import AdvisorConfig, OutcomeAdvisor, logged_explanation
from tests.learning.learning_support import CONF, NOW, SCANNED_ZH, ListSource, file_log, memory_log, sha
from tests.learning.test_cr11_learning import TASK, A, B, enabled, records

PAIRS = tuple(sorted((d.adapter_id, d.version) for d in (A, B)))


def _advise(seam: ScoreAdvisor, binding: str) -> tuple[str, list[object]]:
    expected = seam.context_hash(**TASK, context_features=SCANNED_ZH, candidates=PAIRS, classification=CONF.value,
                                 plan_binding_hash=binding)
    return expected, list(seam.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH,
                                      classification=CONF.value, plan_binding_hash=binding))


def test_issued_returns_the_logged_advice_for_that_binding(tmp_path: Path) -> None:
    advisor = enabled(log=file_log(tmp_path))
    seam: ScoreAdvisor = advisor
    expected, returned = _advise(seam, sha("plan-1"))
    issued = seam.issued(sha("plan-1"), candidates=PAIRS)
    assert issued is not None and returned
    assert [(a.adapter_id, a.score, a.sample_size, a.profile_hash, a.context_hash) for a in issued] == [
        (a.adapter_id, a.score, a.sample_size, a.profile_hash, a.context_hash) for a in returned]  # type: ignore[attr-defined]
    assert advisor.log is not None
    entry = advisor.log.find(sha("plan-1"))[-1]
    assert [a.explanation for a in issued] == [logged_explanation(item) for item in entry.advice]
    assert apply_advice([A.adapter_id, B.adapter_id], issued, context_hash=expected) == apply_advice(
        [A.adapter_id, B.adapter_id], returned, context_hash=expected)  # type: ignore[arg-type]
    # A fresh advisor over the same durable log (a restart) reports the same issued set.
    restarted = OutcomeAdvisor(ListSource(records()), config=AdvisorConfig(enabled=True), log=file_log(tmp_path),
                               clock=lambda: NOW)
    assert restarted.issued(sha("plan-1"), candidates=PAIRS) == issued


def test_issued_is_none_when_nothing_was_issued_for_the_binding(tmp_path: Path) -> None:
    advisor = enabled()
    _advise(advisor, sha("plan-1"))
    assert advisor.issued(sha("plan-2"), candidates=PAIRS) is None
    assert advisor.issued("not-a-binding", candidates=PAIRS) is None
    assert OutcomeAdvisor(ListSource(records())).issued(sha("plan-1"), candidates=PAIRS) is None  # disabled, no log
    unqualified = enabled(source=[])
    _advise(unqualified, sha("plan-3"))
    assert unqualified.issued(sha("plan-3"), candidates=PAIRS) == ()  # issued an empty set (no qualifying profile)


def test_the_latest_issuance_for_a_binding_wins() -> None:
    log = memory_log()
    first = enabled(log=log)
    _advise(first, sha("plan-1"))
    before = first.issued(sha("plan-1"), candidates=PAIRS)
    later = enabled(source=records()[:30], log=log)  # fewer outcomes since: different advice, same binding
    _advise(later, sha("plan-1"))
    after = later.issued(sha("plan-1"), candidates=PAIRS)
    assert before is not None and after is not None and before != after
    assert first.issued(sha("plan-1"), candidates=PAIRS) == after


def test_issued_requires_the_exact_logged_candidate_set() -> None:
    advisor = enabled()
    _advise(advisor, sha("plan-1"))
    assert advisor.issued(sha("plan-1"), candidates=PAIRS) is not None
    assert advisor.issued(sha("plan-1"), candidates=PAIRS[:1]) is None
    assert advisor.issued(sha("plan-1"), candidates=(*PAIRS, ("ocr.engine-c", "1"))) is None
    assert advisor.issued(sha("plan-1"), candidates=(PAIRS[0], (PAIRS[1][0], "9"))) is None


def test_a_tampered_log_issues_nothing(tmp_path: Path) -> None:
    advisor = enabled(log=file_log(tmp_path))
    _advise(advisor, sha("plan-1"))
    path = tmp_path / "learning" / "advice.jsonl"
    line = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    line["advice"][0]["score"] = 0.25
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")
    assert advisor.issued(sha("plan-1"), candidates=PAIRS) is None
