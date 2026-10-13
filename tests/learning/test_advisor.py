"""OutcomeAdvisor: disabled by default, bounded, explainable, logged, re-derivable and reversible."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from pados.capability.seams import ADVICE_BOUND, Advice, ScoreAdvisor, apply_advice
from pados.learning.advice_log import (
    TEST_ONLY_ACKNOWLEDGEMENT,
    AdviceLog,
    AdviceLogEntry,
    AdviceRecordBody,
    AdviceStatus,
)
from pados.learning.advisor import (
    UNBOUND_CONTEXT_HASH,
    AdvisorConfig,
    OutcomeAdvisor,
    VerificationLevel,
    advice_context_hash,
)
from pados.learning.profiles import wilson_lower_bound
from pados.learning.records import OutcomeRecord, adapter_ref_for
from pados.learning.store import LearningStoreError, OutcomeStore
from pados.lifecycle.retention import RetentionPolicy
from tests.learning.learning_support import (
    AUTH,
    BINDING,
    BORN_DIGITAL_EN,
    CONF,
    NOW,
    SCANNED_ZH,
    ListSource,
    adapter,
    file_anchor,
    file_log,
    history,
    memory_log,
    outcome,
)

A, B = adapter("ocr.engine-a"), adapter("ocr.engine-b")
TASK = dict(task_type="DOCUMENT_EXTRACTION", capability_id="DOCUMENT_OCR")


def scanned_zh_history() -> list[OutcomeRecord]:
    """Scanned Traditional-Chinese PDFs: engine A 38/40 good, engine B 25/40 good."""
    return [*history("ocr.engine-a", 38, 2), *history("ocr.engine-b", 25, 15, offset=100)]


def advisor(records: list[OutcomeRecord] | None = None, log: AdviceLog | None = None,
            **config: object) -> OutcomeAdvisor:
    values: dict[str, object] = {"enabled": True, **config}
    return OutcomeAdvisor(ListSource(scanned_zh_history() if records is None else records),
                          config=AdvisorConfig(**values),  # type: ignore[arg-type]
                          log=log or memory_log(), clock=lambda: NOW)


def advise(target: OutcomeAdvisor, features: dict[str, str] = SCANNED_ZH,
           candidates: tuple[object, ...] = (A, B)) -> dict[str, Advice]:
    return {item.adapter_id: item for item in target.advise(**TASK, candidates=candidates,  # type: ignore[arg-type]
                                                            context_features=features, classification=CONF,
                                                            plan_binding_hash=BINDING)}


def ctx(features: dict[str, str] = SCANNED_ZH, candidates: tuple[object, ...] = (A, B)) -> str:
    return advice_context_hash(**TASK, context_features=features, candidates=candidates,  # type: ignore[arg-type]
                               classification=CONF, plan_binding_hash=BINDING)


def entries(target: OutcomeAdvisor) -> tuple[AdviceLogEntry, ...]:
    assert target.log is not None
    return target.log.entries()


def plan(feasible: list[str], base: dict[str, float], adjustments: dict[str, float]) -> list[str]:
    """A stand-in planner: rank feasible candidates by base score plus bounded adjustments."""
    return sorted(feasible, key=lambda name: (-(base[name] + adjustments.get(name, 0.0)), name))


def test_disabled_by_default_returns_no_advice_and_logs_nothing() -> None:
    default = OutcomeAdvisor(ListSource(scanned_zh_history()))
    assert default.config.enabled is False and default.log is None
    assert default.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF) == ()
    quiet = OutcomeAdvisor(ListSource(scanned_zh_history()), log=memory_log())
    assert quiet.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF) == ()
    assert entries(quiet) == ()


def test_scanned_traditional_chinese_pdf_prefers_engine_a() -> None:
    target = advisor()
    result = advise(target)
    assert set(result) == {"ocr.engine-a", "ocr.engine-b"}
    a, b = result["ocr.engine-a"], result["ocr.engine-b"]
    assert a.score > 0 > b.score
    assert all(-ADVICE_BOUND <= item.score <= ADVICE_BOUND for item in result.values())
    assert a.score == round(ADVICE_BOUND * (2 * round(wilson_lower_bound(38, 40), 6) - 1), 6)
    assert a.sample_size == 40 and len(a.profile_hash) == 64
    context_hash = ctx()
    assert a.context_hash == b.context_hash == context_hash
    adjustments = apply_advice(["ocr.engine-a", "ocr.engine-b"], list(result.values()), context_hash=context_hash)
    assert adjustments == {"ocr.engine-a": a.score, "ocr.engine-b": b.score}


def test_explanation_states_sample_size_rates_and_reason_deterministically() -> None:
    first = advise(advisor())["ocr.engine-a"].explanation
    assert first == advise(advisor())["ocr.engine-a"].explanation
    assert len(first) <= 300
    for fragment in ("n=40>=min 20", "good=38/40 (95.0%)", "wilson95_lb=0.835", "verify_pass=95.0% of 40",
                     "human_accept=n/a of 0", "latency p50=1000ms p90=1000ms", "cost_mean=1.000",
                     "score=+0.167 = 0.25*(2*lb-1)"):
        assert fragment in first
    assert "score=-0.015" in advise(advisor())["ocr.engine-b"].explanation


def test_minimum_sample_size_else_no_advice() -> None:
    target = advisor([*history("ocr.engine-a", 10, 0), *history("ocr.engine-b", 25, 0, offset=100)])
    result = advise(target)
    assert set(result) == {"ocr.engine-b"}
    sparse = advisor(history("ocr.engine-a", 10, 0))
    assert advise(sparse) == {}
    assert entries(sparse)[-1].status is AdviceStatus.NO_QUALIFYING_PROFILE
    with pytest.raises(ValueError, match="ADVISOR_MIN_SAMPLES_OUT_OF_RANGE"):
        AdvisorConfig(enabled=True, min_samples=1)


def test_advice_only_for_candidates_passed_in_and_matching_context() -> None:
    records = [*scanned_zh_history(), *history("ocr.engine-c", 40, 0, offset=200)]
    assert set(advise(advisor(records))) == {"ocr.engine-a", "ocr.engine-b"}
    assert set(advise(advisor(records), candidates=(A,))) == {"ocr.engine-a"}
    # A born-digital English PDF is a different bucket: no evidence there, no advice.
    assert advise(advisor(records), features=BORN_DIGITAL_EN) == {}
    # Coarser buckets are an explicit configuration choice.
    assert set(advise(advisor(records, bucket_keys=("scan_type",)), features={"scan_type": "SCANNED"})) == {
        "ocr.engine-a", "ocr.engine-b"}


def test_upgraded_adapter_version_does_not_inherit_history() -> None:
    upgraded = adapter("ocr.engine-a", version="2")
    assert advise(advisor(), candidates=(upgraded, B)).keys() == {"ocr.engine-b"}


def test_advice_for_another_context_is_dropped_by_apply_advice() -> None:
    result = list(advise(advisor()).values())
    other = ctx(BORN_DIGITAL_EN)
    assert apply_advice(["ocr.engine-a", "ocr.engine-b"], result, context_hash=other) == {}


def test_disabling_restores_the_exact_no_advice_plan() -> None:
    feasible, base = ["ocr.engine-a", "ocr.engine-b"], {"ocr.engine-a": 0.50, "ocr.engine-b": 0.60}
    context_hash = ctx()
    baseline = plan(feasible, base, apply_advice(feasible, [], context_hash=context_hash))
    assert baseline == ["ocr.engine-b", "ocr.engine-a"]

    def planned(target: OutcomeAdvisor) -> list[str]:
        advice = target.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                               plan_binding_hash=BINDING)
        return plan(feasible, base, apply_advice(feasible, advice, context_hash=context_hash))

    enabled = advisor()
    assert planned(enabled) == ["ocr.engine-a", "ocr.engine-b"]  # advice moved A up within the bound
    disabled = OutcomeAdvisor(ListSource(scanned_zh_history()), config=AdvisorConfig(enabled=False))
    assert planned(disabled) == baseline
    assert planned(advisor()) != baseline and planned(disabled) == baseline


def test_every_advice_set_is_logged_with_profile_hashes_and_can_be_rederived() -> None:
    records = scanned_zh_history()
    source = ListSource(records)
    target = OutcomeAdvisor(source, config=AdvisorConfig(enabled=True), log=memory_log(), clock=lambda: NOW)
    result = target.advise_logged(**TASK, candidates=[B, A], context_features=SCANNED_ZH, classification=CONF,
                                  plan_binding_hash=BINDING)
    returned = result.advice
    (entry,) = entries(target)
    assert (result.entry_seq, result.entry_hash) == (entry.seq, entry.entry_hash)
    assert entry.status is AdviceStatus.ADVISED and entry.config_hash == target.config.config_hash
    assert [c.adapter_ref for c in entry.candidates] == sorted(adapter_ref_for(d.adapter_id, d.version)
                                                                for d in (A, B))
    assert [a.snapshot.profile_hash for a in entry.advice] == [a.profile_hash for a in returned]
    assert entry.context_hash == ctx()
    assert target.verify(entry).level is VerificationLevel.DERIVATION_VERIFIED
    source.records.append(outcome(500, adapter_id="ocr.engine-a", success=False))
    # records changed: the profile no longer re-derives, but the logged score still follows from its snapshot
    assert target.verify(entry).level is VerificationLevel.SCORE_ARITHMETIC_VERIFIED
    stricter = OutcomeAdvisor(source, config=AdvisorConfig(enabled=True, min_samples=30), log=memory_log())
    with pytest.raises(LearningStoreError, match="ADVISOR_CONFIG_CHANGED"):
        stricter.rederive(entry)


def test_invalid_context_or_candidates_give_no_advice_and_log_no_content() -> None:
    target = advisor()
    leaked = "contract-for-" + "acme.pdf"
    assert target.advise(**TASK, candidates=[A, B], context_features={"file_name": leaked},
                         classification=CONF, plan_binding_hash=BINDING) == ()
    entry = entries(target)[-1]
    assert entry.status is AdviceStatus.CONTEXT_INVALID and entry.context_features == ()
    assert leaked not in json.dumps(entry.model_dump(mode="json"))
    assert target.advise(task_type="summarize this", capability_id="DOCUMENT_OCR", candidates=[A],
                         context_features=SCANNED_ZH, classification=CONF, plan_binding_hash=BINDING) == ()
    assert entries(target)[-1].task_ref is None
    other = adapter("llm.any", capability_id="MODEL_INFERENCE")
    assert target.advise(**TASK, candidates=[A, other], context_features=SCANNED_ZH, classification=CONF,
                         plan_binding_hash=BINDING) == ()
    assert entries(target)[-1].status is AdviceStatus.CANDIDATES_INVALID
    assert target.advise(**TASK, candidates=[A, A], context_features=SCANNED_ZH, classification=CONF,
                         plan_binding_hash=BINDING) == ()


class BrokenSource:
    def read(self) -> tuple[OutcomeRecord, ...]:
        raise LearningStoreError("LEARNING_STORE_CORRUPT")


class BrokenLog(AdviceLog):
    def record(self, body: AdviceRecordBody) -> AdviceLogEntry:
        raise OSError("disk full")


def test_unavailable_source_or_unloggable_advice_gives_no_advice() -> None:
    enabled = AdvisorConfig(enabled=True)
    call = dict(TASK, context_features=SCANNED_ZH, classification=CONF, plan_binding_hash=BINDING)
    broken = OutcomeAdvisor(BrokenSource(), config=enabled, log=memory_log(), clock=lambda: NOW)
    assert broken.advise(candidates=[A], **call) == ()  # type: ignore[arg-type]
    assert entries(broken)[-1].status is AdviceStatus.SOURCE_UNAVAILABLE
    assert broken.verify(entries(broken)[-1]).ok
    duplicated = OutcomeAdvisor(ListSource([*scanned_zh_history(), outcome(0)]), config=enabled, log=memory_log(),
                                clock=lambda: NOW)
    assert duplicated.advise(candidates=[A], **call) == ()  # type: ignore[arg-type]
    assert entries(duplicated)[-1].status is AdviceStatus.SOURCE_UNAVAILABLE
    naive = OutcomeAdvisor(ListSource(scanned_zh_history()), config=enabled, log=memory_log(),
                           clock=lambda: NOW.replace(tzinfo=None))
    assert naive.advise(candidates=[A, B], **call) == ()  # type: ignore[arg-type]
    silent = OutcomeAdvisor(ListSource(scanned_zh_history()), config=enabled,
                            log=BrokenLog.ephemeral_for_tests(acknowledgement=TEST_ONLY_ACKNOWLEDGEMENT))
    assert silent.advise(candidates=[A, B], **call) == ()  # type: ignore[arg-type]


def test_persisted_advice_log_is_bounded_and_round_trips(tmp_path: Path) -> None:
    log = file_log(tmp_path, max_entries=3)
    target = OutcomeAdvisor(ListSource(scanned_zh_history()), config=AdvisorConfig(enabled=True), log=log,
                            clock=lambda: NOW)
    for features in (SCANNED_ZH, SCANNED_ZH, BORN_DIGITAL_EN, SCANNED_ZH):
        log.prune()  # the operator's purge (CR-11d OL-14: the log never deletes on its own)
        target.advise(**TASK, candidates=[A, B], context_features=features, classification=CONF,
                      plan_binding_hash=BINDING)
    assert len((tmp_path / "learning" / "advice.jsonl").read_text().splitlines()) <= 3  # marker included
    reopened = file_log(tmp_path).entries()
    assert [e.status for e in reopened] == [AdviceStatus.NO_QUALIFYING_PROFILE, AdviceStatus.ADVISED]
    assert target.verify(reopened[-1]).ok and file_log(tmp_path).verify().ok


def test_outcome_store_is_an_advice_source(tmp_path: Path) -> None:
    store = OutcomeStore(tmp_path / "learning" / "outcomes.jsonl", authenticator=AUTH, clock=lambda: NOW)
    for record in scanned_zh_history():
        store.append(record)
    target = OutcomeAdvisor(store, config=AdvisorConfig(enabled=True), log=file_log(tmp_path), clock=lambda: NOW)
    assert advise(target) == advise(advisor())


def test_documented_example_matches_the_code() -> None:
    doc = (Path(__file__).resolve().parents[2] / "docs" / "learning" / "OUTCOME_LEARNING_V1.md").read_text()
    result = advise(advisor())
    explanation = result["ocr.engine-a"].explanation
    assert explanation.replace("human_accept=n/a of 0; ", "human_accept=n/a of 0;\n") in doc
    assert f"{result['ocr.engine-a'].score:+.3f}" == "+0.167" and f"{result['ocr.engine-b'].score:+.3f}" == "-0.015"


def test_persisted_advice_log_follows_r1_retention(tmp_path: Path) -> None:
    now = [NOW]
    path = tmp_path / "learning" / "advice.jsonl"
    log = AdviceLog(path, retention=RetentionPolicy.configured(r1_days=7), clock=lambda: now[0],
                    anchor=file_anchor(tmp_path))
    target = OutcomeAdvisor(ListSource(scanned_zh_history()), config=AdvisorConfig(enabled=True), log=log,
                            clock=lambda: now[0])
    target.advise(**TASK, candidates=[A, B], context_features=SCANNED_ZH, classification=CONF,
                  plan_binding_hash=BINDING)
    now[0] = NOW + timedelta(days=8)
    assert log.entries() == ()
    assert len(path.read_text().splitlines()) == 1  # the read deleted the expired entry: only the PRUNED marker
    target.advise(**TASK, candidates=[A, B], context_features=BORN_DIGITAL_EN, classification=CONF,
                  plan_binding_hash=BINDING)
    assert len(path.read_text().splitlines()) == 2  # PRUNED marker + the new entry
    assert log.verify().ok
    path.write_text(path.read_text().replace('"classification":"CONFIDENTIAL"', '"classification":"free text"'))
    with pytest.raises(LearningStoreError, match="LEARNING_STORE_CORRUPT"):
        log.entries()

def test_advisor_implements_the_seam_context_hash() -> None:
    seam: ScoreAdvisor = OutcomeAdvisor(ListSource([]))
    features = {"document_language": "zh-Hant", "scan_type": "SCANNED"}
    # Without candidates, classification AND plan binding the hash is unbound: it matches no advice.
    assert seam.context_hash(**TASK, context_features=features) == UNBOUND_CONTEXT_HASH  # type: ignore[call-arg]
    unbound = OutcomeAdvisor(ListSource([])).context_hash(**TASK, context_features=features, candidates=[A, B],
                                                            classification=CONF)
    assert unbound == UNBOUND_CONTEXT_HASH
    bound = OutcomeAdvisor(ListSource([])).context_hash(**TASK, context_features=features, candidates=[A, B],
                                                          classification=CONF, plan_binding_hash=BINDING)
    assert bound == ctx(features) != UNBOUND_CONTEXT_HASH
