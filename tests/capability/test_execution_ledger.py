"""CR-16 H-01: the trusted execution ledger - executor-written, append-only, hash-chained, owner-only, fail closed.

CR-16b: H-04 (a read-only reader with no append surface; consumers require an anchored reader; optional
HMAC entry authentication under an executor key reference) and H-07 (an append that would cross
``MAX_LEDGER_BYTES`` is refused before anything is written).

``os.write`` is replaced (for the ledger module only) by a scripted proxy, so short, zero and failing writes are
exercised deterministically."""
from __future__ import annotations

import ast
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fabric_support import fabric

import pados
import pados.capability.execution_ledger as ledger_module
from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.execution_ledger import (
    ENTRY_KEYS,
    ExecutionLedgerError,
    ExecutionLedgerReader,
    FileExecutionLedger,
    LedgerCheckpoint,
    LedgerStage,
    append_execution,
    require_reader,
    stage_bindings_hash,
)
from pados.capability.executor import ExecutionRefused, ExecutionStatus
from pados.capability.receipts import chain
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary
from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import assert_minimized

SRC = Path(pados.__file__).resolve().parent
LEDGER = SRC / "capability" / "execution_ledger.py"
LATER = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def ledger_at(tmp_path: Path, **kwargs: Any) -> FileExecutionLedger:
    return FileExecutionLedger(tmp_path / "ledger" / "executions.jsonl", **kwargs)


def lines(ledger: FileExecutionLedger) -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in ledger.path.read_bytes().splitlines()]


def rewrite(ledger: FileExecutionLedger, entries: list[dict[str, Any]], *, reseal: bool) -> None:
    """Replace the file content (an attacker with file access); ``reseal`` recomputes the whole chain."""
    prev = "0" * 64
    out = []
    for entry in entries:
        if reseal:
            entry = {**entry, "prev_hash": prev}
            entry["entry_hash"] = canonical_hash({k: v for k, v in entry.items() if k != "entry_hash"})
            prev = entry["entry_hash"]
        out.append(json.dumps(entry, sort_keys=True, separators=(",", ":")))
    ledger.path.write_bytes(("\n".join(out) + "\n").encode("ascii"))


class MemoryAnchor:
    """A compare-and-set anchor kept outside the ledger's store (test stand-in for the operator's anchor)."""

    def __init__(self) -> None:
        self.head: LedgerCheckpoint | None = None
        self.fail = False

    def record(self, previous: LedgerCheckpoint | None, new: LedgerCheckpoint) -> None:
        if self.fail:
            raise OSError("anchor offline")
        if previous != self.head or new.seq != (1 if previous is None else previous.seq + 1):
            raise ValueError("ANCHOR_CONFLICT")
        self.head = new

    def latest(self) -> LedgerCheckpoint | None:
        return self.head


class OsProxy:
    def __init__(self, write: Callable[[int, Any], int]) -> None:
        self._write = write
        self.events: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def write(self, fd: int, data: Any) -> int:
        self.events.append("write")
        return self._write(fd, data)

    def fsync(self, fd: int) -> None:
        self.events.append("fsync")
        os.fsync(fd)


def scripted(*steps: str) -> Callable[[int, Any], int]:
    queue = list(steps)

    def write(fd: int, data: Any) -> int:
        step = queue.pop(0) if queue else "all"
        view = bytes(data)
        if step == "byte":
            return os.write(fd, view[:1])
        if step == "half":
            return os.write(fd, view[:max(1, len(view) // 2)])
        if step == "zero":
            return 0
        if step == "error":
            raise OSError(28, "No space left on device")
        return os.write(fd, view)

    return write


def run_once(tmp_path: Path, ledger: FileExecutionLedger, **task: Any) -> Any:
    fab = fabric(tmp_path)
    plan = fab.planner().plan(fab.task(**task))
    return fab, plan, fab.executor(ledger=ledger).execute(plan, fab.task(**task))


def test_without_a_ledger_the_executor_is_unchanged_and_writes_nothing(tmp_path: Path) -> None:
    fab = fabric(tmp_path)
    plan = fab.planner().plan(fab.task())
    result = fab.executor().execute(plan, fab.task())
    assert result.status is ExecutionStatus.COMPLETED
    assert not (tmp_path / "ledger").exists()
    with pytest.raises(ValueError, match="EXECUTION_LEDGER_INVALID"):
        fab.executor(ledger=object())


def test_every_completed_and_failed_execution_is_recorded_content_free(tmp_path: Path) -> None:
    ledger = ledger_at(tmp_path)
    fab, plan, result = run_once(tmp_path, ledger)
    task = fab.task()
    entry = ledger.resolve(result.plan_receipt.envelope_hash)
    assert entry is not None and entry.matches(result.plan_receipt) and entry.status == "COMPLETED"
    assert (entry.seq, entry.plan_id, entry.plan_hash, entry.task_hash, entry.task_id, entry.task_type) == (
        1, plan.plan_id, plan.plan_hash, task.task_hash, task.task_id, task.task_type)
    assert entry.bindings_hash == stage_bindings_hash(plan.template_id, plan.template_hash, [
        (stage.stage_id, stage.capability_id, stage.inputs) for stage in plan.stages])
    assert entry.stages == tuple(LedgerStage.of(stage) for stage in result.plan_receipt.stages)
    (raw,) = lines(ledger)
    assert set(raw) == ENTRY_KEYS
    assert_minimized(raw, allowed_keys=ENTRY_KEYS)
    text = ledger.path.read_text(encoding="ascii")
    assert "scanned page" not in text and "chunk" in text  # ids and hashes, never content

    def broken(request: StageRequest, *, context: StageContext) -> StageOutput:
        raise CapabilityAdapterError("CHUNKER_DOWN")

    fab.adapters["chunk.fixed"].execute = broken  # type: ignore[method-assign]
    failed = fab.executor(ledger=ledger).execute(plan, task)
    assert failed.status is ExecutionStatus.FAILED
    recorded = ledger.resolve(failed.plan_receipt.envelope_hash)
    assert recorded is not None and recorded.status == "FAILED" and recorded.seq == 2
    assert recorded.matches(failed.plan_receipt) and not recorded.matches(result.plan_receipt)
    if sys.platform != "win32":
        assert oct(ledger.path.stat().st_mode & 0o777) == "0o600"
        assert oct(ledger.path.parent.stat().st_mode & 0o777) == "0o700"


def test_a_fabricated_internally_consistent_chain_is_not_in_the_ledger(tmp_path: Path) -> None:
    ledger = ledger_at(tmp_path)
    _, plan, result = run_once(tmp_path, ledger)
    receipt = result.plan_receipt
    stages = list(receipt.stages)
    stages[0] = stages[0].model_copy(update={"verifier_id": "forged.verifier"})
    forged = receipt.model_copy(update={"stages": tuple(chain(plan.plan_id, plan.plan_hash, stages))})
    assert forged.verify_structure() and forged.envelope_hash != receipt.envelope_hash
    assert ledger.resolve(forged.envelope_hash) is None
    entry = ledger.resolve(receipt.envelope_hash)
    assert entry is not None and not entry.matches(forged)


def test_the_same_execution_is_recorded_once_and_the_ledger_is_bounded(tmp_path: Path) -> None:
    ledger = ledger_at(tmp_path, capacity=2)
    fab, plan, result = run_once(tmp_path, ledger)
    task = fab.task()
    (first,) = ledger.entries()
    bindings = first.bindings_hash
    again = append_execution(ledger, receipt=result.plan_receipt, task_id=task.task_id, task_type=task.task_type,
                             task_hash=task.task_hash, bindings_hash=bindings, status="COMPLETED",
                             recorded_at=LATER)
    assert again == first and len(lines(ledger)) == 1  # identical evidence is recorded once
    other = fab.task(task_id="task-2")
    fab.executor(ledger=ledger).execute(fab.planner().plan(other), other)
    assert len(ledger.entries()) == 2
    third = fab.task(task_id="task-3")
    with pytest.raises(ExecutionRefused, match="^EXECUTION_LEDGER_FULL$"):
        fab.executor(ledger=ledger).execute(fab.planner().plan(third), third)


def test_a_rewritten_entry_fails_closed_for_readers_and_the_writer(tmp_path: Path) -> None:
    ledger = ledger_at(tmp_path)
    fab, plan, result = run_once(tmp_path, ledger)
    (entry,) = lines(ledger)
    rewrite(ledger, [{**entry, "status": "FAILED"}], reseal=False)
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_CORRUPT$"):
        ledger.resolve(result.plan_receipt.envelope_hash)
    assert ledger.verify() is False
    with pytest.raises(ExecutionRefused, match="^EXECUTION_LEDGER_CORRUPT$"):
        fab.executor(ledger=ledger).execute(plan, fab.task())
    rewrite(ledger, [entry, entry], reseal=False)  # a replayed line breaks seq and chain
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_CORRUPT$"):
        ledger.entries()
    rewrite(ledger, [{**entry, "note": "x"}], reseal=True)  # a foreign field, even with a recomputed chain
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_CORRUPT$"):
        ledger.entries()
    ledger.path.write_bytes((json.dumps(entry, sort_keys=True) + "\n").encode("ascii"))  # same data, not canonical
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_CORRUPT$"):
        ledger.entries()
    rewrite(ledger, [entry], reseal=False)  # the original line verifies again
    assert ledger.verify() is True


def test_a_torn_tail_fails_closed_and_is_never_truncated(tmp_path: Path) -> None:
    ledger = ledger_at(tmp_path)
    fab, plan, result = run_once(tmp_path, ledger)
    with ledger.path.open("ab") as handle:
        handle.write(b'{"schema":"pados.capability.execution-ledger/v1","seq":2')
    before = ledger.path.read_bytes()
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_TORN_ENTRY$"):
        ledger.resolve(result.plan_receipt.envelope_hash)
    with pytest.raises(ExecutionRefused, match="^EXECUTION_LEDGER_TORN_ENTRY$"):
        fab.executor(ledger=ledger).execute(plan, fab.task())
    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize("steps", [("half", "zero"), ("byte", "error")])
def test_an_incomplete_append_refuses_and_leaves_a_torn_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             steps: tuple[str, ...]) -> None:
    ledger = ledger_at(tmp_path)
    fab = fabric(tmp_path)
    plan = fab.planner().plan(fab.task())
    proxy = OsProxy(scripted(*steps))
    monkeypatch.setattr(ledger_module, "os", proxy)
    with pytest.raises(ExecutionRefused, match="^EXECUTION_LEDGER_UNAVAILABLE$"):
        fab.executor(ledger=ledger).execute(plan, fab.task())
    assert "fsync" not in proxy.events  # never fsync an incomplete entry
    monkeypatch.setattr(ledger_module, "os", os)
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_TORN_ENTRY$"):
        ledger.entries()


def test_short_writes_are_completed_before_fsync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = ledger_at(tmp_path)
    fab = fabric(tmp_path)
    plan = fab.planner().plan(fab.task())
    proxy = OsProxy(scripted("byte", "half", "byte"))
    monkeypatch.setattr(ledger_module, "os", proxy)
    result = fab.executor(ledger=ledger).execute(plan, fab.task())
    assert proxy.events[-1] == "fsync" and proxy.events.count("write") >= 4
    monkeypatch.setattr(ledger_module, "os", os)
    entry = ledger.resolve(result.plan_receipt.envelope_hash)
    assert entry is not None and entry.matches(result.plan_receipt)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes and links")
def test_an_unsafe_or_linked_ledger_file_is_refused(tmp_path: Path) -> None:
    ledger = ledger_at(tmp_path)
    _, _, result = run_once(tmp_path, ledger)
    ledger.path.chmod(0o644)
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_UNSAFE$"):
        ledger.resolve(result.plan_receipt.envelope_hash)
    ledger.path.chmod(0o600)
    target = ledger.path.with_name("real.jsonl")
    ledger.path.rename(target)
    ledger.path.symlink_to(target)
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_UNSAFE$"):
        ledger.resolve(result.plan_receipt.envelope_hash)


def test_an_anchored_ledger_refuses_a_whole_file_rewrite_with_a_recomputed_chain(tmp_path: Path) -> None:
    anchor = MemoryAnchor()
    ledger = ledger_at(tmp_path, anchor=anchor)
    fab, plan, result = run_once(tmp_path, ledger)
    (entry,) = lines(ledger)
    assert anchor.head == LedgerCheckpoint(1, entry["entry_hash"])
    rewrite(ledger, [{**entry, "task_id": "task-forged"}], reseal=True)
    unanchored = ledger_at(tmp_path)
    assert unanchored.verify() is True  # without an anchor a full re-seal is the known residual
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_ANCHOR_MISMATCH$"):
        ledger.resolve(result.plan_receipt.envelope_hash)
    ledger.path.unlink()  # deleting the file does not reset an anchored ledger either
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_ANCHOR_MISMATCH$"):
        ledger.entries()


def test_a_failed_anchor_refuses_the_execution_and_later_reads_fail_closed(tmp_path: Path) -> None:
    anchor = MemoryAnchor()
    ledger = ledger_at(tmp_path, anchor=anchor)
    fab = fabric(tmp_path)
    plan = fab.planner().plan(fab.task())
    anchor.fail = True
    with pytest.raises(ExecutionRefused, match="^EXECUTION_LEDGER_ANCHOR_UNAVAILABLE$"):
        fab.executor(ledger=ledger).execute(plan, fab.task())
    anchor.fail = False
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_ANCHOR_MISMATCH$"):
        ledger.entries()  # the file is one entry ahead of the anchor until an operator repairs it


def test_the_writer_refuses_foreign_ledgers_and_unstructured_receipts(tmp_path: Path) -> None:
    ledger = ledger_at(tmp_path)
    _, plan, result = run_once(tmp_path, ledger)
    receipt = result.plan_receipt
    kwargs: dict[str, Any] = dict(receipt=receipt, task_id="task-1", task_type="doc.extract", task_hash="a" * 64,
                                  bindings_hash="b" * 64, status="COMPLETED", recorded_at=LATER)
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_INVALID$"):
        append_execution(object(), **kwargs)  # type: ignore[arg-type]
    broken = receipt.model_copy(update={"stages": tuple(reversed(receipt.stages))})
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_ENTRY_INVALID$"):
        append_execution(ledger, **{**kwargs, "receipt": broken})
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_ENTRY_INVALID$"):
        append_execution(ledger, **{**kwargs, "status": "ACCEPTED"})
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_CONFLICT$"):
        append_execution(ledger, **kwargs)  # same envelope, different task binding


def _names(path: Path) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            used.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            used.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
    return used


def test_only_the_executor_imports_the_writer() -> None:
    """The executor is the only writer: within src/pados only executor.py names ``append_execution`` and no
    module but the ledger itself reaches the private append."""
    writers = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
               if path != LEDGER and "append_execution" in _names(path)}
    assert writers == {"capability/executor.py"}
    private = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
               if path != LEDGER and {"_append_entry", "_append", "_write_all"} & _names(path)
               and "execution_ledger" in path.read_text(encoding="utf-8")}
    assert private == set()
    relative = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig")))
                if isinstance(node, ast.ImportFrom) and node.level > 0
                and (node.module or "").split(".")[-1] == "execution_ledger"}
    assert relative == set()


# --- CR-16b H-04: read-only readers, mandatory anchoring for consumers, optional entry MACs --------------

KEY_NAME = "PADOS_TEST_EXECUTION_LEDGER_MAC"
KEY_REF = f"secretref://env/{KEY_NAME}"


def _boundary(value: str) -> SecretBoundary:
    return SecretBoundary(EnvironmentSecretResolver({KEY_NAME: value}))


def test_the_reader_has_no_append_surface_and_no_reference_to_the_writer(tmp_path: Path) -> None:
    anchor = MemoryAnchor()
    ledger = ledger_at(tmp_path, anchor=anchor)
    _, _, result = run_once(tmp_path, ledger)
    reader = ledger.reader()
    assert type(reader) is ExecutionLedgerReader and not isinstance(reader, FileExecutionLedger)
    surface = {name for cls in type(reader).__mro__ for name in vars(cls)}
    assert not surface & {"_append_entry", "_append", "append", "record", "reader", "_writer_anchor"}
    assert not hasattr(reader, "__dict__")  # slots only: nothing can be attached to it
    held = [getattr(reader, slot) for cls in type(reader).__mro__ for slot in getattr(cls, "__slots__", ())]
    assert not any(isinstance(value, FileExecutionLedger) for value in held)
    assert reader.resolve(result.plan_receipt.envelope_hash) is not None and reader.anchored
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_INVALID$"):
        append_execution(reader, receipt=result.plan_receipt, task_id="t", task_type="doc.extract",  # type: ignore[arg-type]
                         task_hash="a" * 64, bindings_hash="b" * 64, status="COMPLETED", recorded_at=LATER)
    with pytest.raises(ValueError, match="EXECUTION_LEDGER_INVALID"):
        fabric(tmp_path / "other").executor(ledger=reader)  # a reader is never a writer either


def test_consumers_require_an_anchored_reader_and_refuse_a_writer(tmp_path: Path) -> None:
    anchored = ledger_at(tmp_path, anchor=MemoryAnchor())
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_READER_REQUIRED$"):
        require_reader(anchored)
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_READER_REQUIRED$"):
        require_reader(object())
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_ANCHOR_REQUIRED$"):
        require_reader(ExecutionLedgerReader(anchored.path))
    assert require_reader(anchored.reader()).anchored


def _keyed(tmp_path: Path, anchor: MemoryAnchor, key: str = "k" * 48) -> FileExecutionLedger:
    return ledger_at(tmp_path, anchor=anchor, mac_key_ref=KEY_REF, boundary=_boundary(key))


def test_an_hmac_forged_entry_is_refused_even_with_a_matching_anchor(tmp_path: Path) -> None:
    anchor = MemoryAnchor()
    ledger = _keyed(tmp_path, anchor)
    _, _, result = run_once(tmp_path, ledger)
    reader = ledger.reader()
    assert reader.authenticated and reader.resolve(result.plan_receipt.envelope_hash) is not None
    (entry,) = lines(ledger)
    assert set(entry) == ENTRY_KEYS | {"entry_mac"}
    forged = {**entry, "task_id": "task-forged", "prev_hash": "0" * 64}  # no key: a guessed MAC
    forged["entry_hash"] = canonical_hash({k: v for k, v in forged.items() if k not in {"entry_hash", "entry_mac"}})
    forged["entry_mac"] = "f" * 64
    rewrite(ledger, [forged], reseal=False)
    anchor.head = LedgerCheckpoint(1, forged["entry_hash"])  # the attacker controls the anchor too
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_MAC_INVALID$"):
        reader.entries()
    unkeyed = {k: v for k, v in forged.items() if k != "entry_mac"}
    rewrite(ledger, [unkeyed], reseal=False)  # dropping the MAC does not help either
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_MAC_INVALID$"):
        reader.entries()
    rewrite(ledger, [entry], reseal=False)
    anchor.head = LedgerCheckpoint(1, entry["entry_hash"])
    assert len(reader.entries()) == 1
    other_key = ExecutionLedgerReader(ledger.path, anchor=anchor, mac_key_ref=KEY_REF, boundary=_boundary("x" * 48))
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_MAC_INVALID$"):
        other_key.entries()


def test_an_unavailable_or_short_key_refuses_and_the_key_is_never_persisted(tmp_path: Path) -> None:
    anchor = MemoryAnchor()
    key = "k" * 48
    ledger = _keyed(tmp_path, anchor, key)
    run_once(tmp_path, ledger)
    assert key not in ledger.path.read_text(encoding="ascii")
    short = ExecutionLedgerReader(ledger.path, anchor=anchor, mac_key_ref=KEY_REF, boundary=_boundary("k" * 8))
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_KEY_UNAVAILABLE$"):
        short.entries()
    missing = ExecutionLedgerReader(ledger.path, anchor=anchor, mac_key_ref=KEY_REF,
                                    boundary=SecretBoundary(EnvironmentSecretResolver({})))
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_KEY_UNAVAILABLE$"):
        missing.entries()
    for kwargs in ({"mac_key_ref": KEY_REF}, {"boundary": _boundary(key)},
                   {"mac_key_ref": "not-a-ref", "boundary": _boundary(key)}):
        with pytest.raises(ValueError, match="^EXECUTION_LEDGER_MAC_CONFIG_INVALID$"):
            ExecutionLedgerReader(ledger.path, **kwargs)


# --- CR-16b H-07: the file-size bound is checked before writing ------------------------------------------

def test_an_append_that_would_cross_the_file_bound_is_refused_before_writing(tmp_path: Path,
                                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    fab = fabric(tmp_path)
    receipts = []
    for task_id in ("task-1", "task-2"):
        task = fab.task(task_id=task_id)
        receipts.append((task, fab.executor().execute(fab.planner().plan(task), task).plan_receipt))

    def append(ledger: FileExecutionLedger, index: int) -> None:
        task, receipt = receipts[index]
        append_execution(ledger, receipt=receipt, task_id=task.task_id, task_type=task.task_type,
                         task_hash=task.task_hash, bindings_hash="b" * 64, status="COMPLETED", recorded_at=LATER)

    probe = FileExecutionLedger(tmp_path / "probe" / "executions.jsonl")
    append(probe, 0)
    first = probe.path.stat().st_size
    append(probe, 1)
    both = probe.path.stat().st_size
    monkeypatch.setattr(ledger_module, "MAX_LEDGER_BYTES", both - 1)  # one byte short of the second entry
    short = FileExecutionLedger(tmp_path / "short" / "executions.jsonl")
    append(short, 0)
    with pytest.raises(ExecutionLedgerError, match="^EXECUTION_LEDGER_FULL$"):
        append(short, 1)
    assert short.path.stat().st_size == first and len(short.entries()) == 1  # nothing written; still readable
    monkeypatch.setattr(ledger_module, "MAX_LEDGER_BYTES", both)  # exactly at the bound: accepted
    exact = FileExecutionLedger(tmp_path / "exact" / "executions.jsonl")
    append(exact, 0)
    append(exact, 1)
    assert exact.path.stat().st_size == both and len(exact.entries()) == 2
