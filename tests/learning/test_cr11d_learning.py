"""CR-11d OL-14: Outcome Learning never deletes on its own. Reads, advice issuance and appends leave every stored
byte and the external anchor untouched; the operator-invoked ``prune()`` is the only deletion path."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from pados.learning.store import LearningStoreError
from tests.learning.learning_support import NOW, TrustedReceipts, file_log, outcome
from tests.learning.test_cr11_learning import Clock
from tests.learning.test_cr11b_learning import store
from tests.learning.test_cr11c_learning import Anchor, advise, advisor, log_path


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file() and p.suffix != ".lock"}


def test_reads_and_issuance_never_modify_the_advice_log_or_its_anchor(tmp_path: Path) -> None:
    clock = Clock(NOW)
    anchor = Anchor()
    log = file_log(tmp_path, anchor=anchor, clock=clock, max_entries=3)
    target = advisor(log, clock=clock)
    for index in range(3):
        advise(target, f"{index:064x}")
    clock.now = NOW + timedelta(days=15)  # every entry expired; the log is also at capacity
    before, head, calls = log_path(tmp_path).read_bytes(), anchor.latest(), len(anchor.calls)
    assert log.entries() == ()
    log.find(f"{0:064x}")
    log.anchored_view()
    target.issued(f"{0:064x}", candidates=())
    log.verify()
    assert log_path(tmp_path).read_bytes() == before and anchor.latest() == head and len(anchor.calls) == calls
    assert advise(target, f"{9:064x}").audit_ref is None  # full: refused, nothing deleted to make room
    assert log_path(tmp_path).read_bytes() == before
    assert log.prune() == 3 and log_path(tmp_path).read_bytes() != before  # the operator's purge only


def test_outcome_reads_and_appends_never_delete(tmp_path: Path) -> None:
    auth, clock = TrustedReceipts(), Clock(NOW)
    outcomes = store(tmp_path, auth, clock, max_records=2)
    outcomes.append(outcome(1, auth=auth))
    outcomes.append(outcome(2, auth=auth))
    clock.now = NOW + timedelta(days=15)
    before = _files(tmp_path)
    assert outcomes.read() == () and outcomes.read_checked().pruned == 0
    with pytest.raises(LearningStoreError, match="^OUTCOME_RECORD_ALREADY_EXPIRED$"):
        outcomes.append(outcome(3, auth=auth))
    assert _files(tmp_path) == before
    assert outcomes.prune() == 2 and outcomes.path.read_text() == ""
