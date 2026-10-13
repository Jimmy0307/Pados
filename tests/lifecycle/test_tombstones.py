import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import DataMinimizationViolation
from pados.lifecycle.tombstones import Phase, TombstoneError, TombstoneLedger, tombstone_record, validate_tombstone
from tests.windows_links import create_link_or_skip


def _record(index: int = 0, phase: Phase = Phase.COMPLETED, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "phase": phase,
        "artifact_hash": hashlib.sha256(f"artifact-{index}".encode()).hexdigest(),
        "status": "REJECTED",
        "timestamp": "2026-09-01T00:00:00+00:00",
        "receipt_ref": f"state_proof_results:r-{index}",
        "decision_evidence_ref": f"state_proof_results:r-{index}:FAILED",
        "recorded_at": "2026-10-02T12:00:00+00:00",
        "reason": "REJECTED_PAYLOAD_RETENTION_ELAPSED",
        "plan_hash": hashlib.sha256(b"plan").hexdigest(),
        "reserved": (1, 2, 24),
    }
    values.update(overrides)
    return tombstone_record(**values)


def _ledger(tmp_path: Path, count: int = 3) -> TombstoneLedger:
    ledger = TombstoneLedger(tmp_path / "t" / "tombstones.jsonl")
    for index in range(count):
        ledger.append(_record(index))
    return ledger


def test_chain_verifies_and_links_entries(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    entries = ledger.entries()
    assert entries[0]["prev_hash"] == "0" * 64
    assert [e["prev_hash"] for e in entries[1:]] == [e["entry_hash"] for e in entries[:-1]]
    assert ledger.verify()
    found = ledger.latest(_record(1)["artifact_hash"])
    assert found is not None and found["payload_state"] == "METADATA_RETAINED_PAYLOAD_PURGED"
    assert ledger.latest("f" * 64) is None
    assert TombstoneLedger(tmp_path / "empty.jsonl").verify()


def test_phases_bind_payload_state() -> None:
    pending = _record(0, Phase.PENDING)
    assert pending["payload_state"] == "PRESENT" and pending["purged_at"] is None
    completed = _record(0, Phase.COMPLETED)
    assert completed["purged_at"] == completed["recorded_at"]
    with pytest.raises(TombstoneError, match="PAYLOAD_STATE"):
        validate_tombstone({**pending, "payload_state": "METADATA_RETAINED_PAYLOAD_PURGED"})
    with pytest.raises(TombstoneError, match="TIMESTAMP"):
        validate_tombstone({**pending, "purged_at": pending["recorded_at"]})
    with pytest.raises(TombstoneError, match="PHASE"):
        validate_tombstone({**pending, "phase": "DONE"})


@pytest.mark.parametrize("mutation", ["edit_status", "edit_hash", "drop_line", "swap_lines", "garbage"])
def test_tampering_is_detected(tmp_path: Path, mutation: str) -> None:
    ledger = _ledger(tmp_path)
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    if mutation == "edit_status":
        entry = json.loads(lines[1])
        entry["record"]["status"] = "ACCEPTED"
        lines[1] = json.dumps(entry, sort_keys=True)
    elif mutation == "edit_hash":
        entry = json.loads(lines[0])
        entry["record"]["artifact_hash"] = "0" * 64
        lines[0] = json.dumps(entry, sort_keys=True)
    elif mutation == "drop_line":
        del lines[1]
    elif mutation == "swap_lines":
        lines[0], lines[1] = lines[1], lines[0]
    else:
        lines.append("{not json")
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not ledger.verify()


def test_cr02_f08_forged_or_corrupt_entries_fail_closed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path, count=1)
    target = _record(7)
    forged = {"prev_hash": "1" * 64, "record": target, "entry_hash": "2" * 64}
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(forged) + "\n")
    for call in (lambda: ledger.latest(target["artifact_hash"]), lambda: ledger.append(_record(8)),
                 ledger.pending):
        with pytest.raises(TombstoneError, match="TOMBSTONE_CHAIN_INVALID"):
            call()
    # A correctly re-hashed forgery carrying an invalid record is refused as well.
    other = TombstoneLedger(tmp_path / "other.jsonl")
    entry: dict[str, Any] = {"prev_hash": "0" * 64, "record": {**_record(0), "payload_state": "PRESENT"}}
    entry["entry_hash"] = canonical_hash(entry)
    other.path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    assert not other.verify()


def test_cr02_f09_concurrent_writers_keep_one_chain(tmp_path: Path) -> None:
    path = tmp_path / "shared" / "tombstones.jsonl"
    errors: list[BaseException] = []

    def writer(offset: int) -> None:
        ledger = TombstoneLedger(path)  # separate instance: only the file lock serializes writers
        try:
            for index in range(10):
                ledger.append(_record(offset * 100 + index))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    ledger = TombstoneLedger(path)
    assert ledger.verify() and len(ledger.entries()) == 40


def test_cr02_f10_ledger_creation_syncs_parent_and_tracks_pending(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    import pados.lifecycle.tombstones as module

    synced: list[Path] = []
    monkeypatch.setattr(module, "fsync_directory", lambda path: synced.append(Path(path)))
    ledger = TombstoneLedger(tmp_path / "new" / "tombstones.jsonl")
    ledger.append(_record(0, Phase.PENDING))
    ledger.append(_record(0, Phase.COMPLETED))
    assert synced == [tmp_path / "new"]  # once, when the ledger file is created
    assert [r["record"]["phase"] for r in ledger.entries()] == ["PENDING", "COMPLETED"]
    assert ledger.pending() == []
    ledger.append(_record(1, Phase.PENDING))
    assert [r["artifact_hash"] for r in ledger.pending()] == [_record(1)["artifact_hash"]]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"artifact_hash": "XYZ"}, "TOMBSTONE_HASH_INVALID"),
        ({"receipt_ref": "receipt with spaces"}, "TOMBSTONE_REFERENCE_INVALID"),
        ({"reason": "free text reason"}, "TOMBSTONE_CODE_INVALID"),
        ({"decision_evidence_ref": "tok" + "en=abcd1234efgh"}, "TOMBSTONE_REFERENCE_INVALID"),
    ],
)
def test_invalid_tombstones_are_refused(overrides: dict[str, str], code: str) -> None:
    with pytest.raises(TombstoneError, match=code):
        _record(0, **overrides)


def test_append_enforces_schema_and_minimization(tmp_path: Path) -> None:
    ledger = TombstoneLedger(tmp_path / "tombstones.jsonl")
    with pytest.raises(TombstoneError, match="TOMBSTONE_SCHEMA_MISMATCH"):
        ledger.append({**_record(0), "context": "entire conversation"})
    with pytest.raises(TombstoneError, match="TOMBSTONE_PAYLOAD_STATE_INVALID"):
        ledger.append({**_record(0), "payload_state": "PRESENT"})
    with pytest.raises(DataMinimizationViolation, match="CREDENTIAL_SHAPED_VALUE"):
        # Assembled at runtime so the release secret scan does not flag the fixture itself.
        ledger.append({**_record(0), "receipt_ref": "sk" + "-abcdefghijklmnop"})
    with pytest.raises(TombstoneError, match="TOMBSTONE_TIMESTAMP_INVALID"):
        ledger.append({**_record(0), "timestamp": "2026-09-01T00:00:00"})
    assert not ledger.path.exists()


def test_linked_ledger_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("", encoding="utf-8")
    create_link_or_skip(target, tmp_path / "tombstones.jsonl", directory=False)
    ledger = TombstoneLedger(tmp_path / "tombstones.jsonl")
    with pytest.raises(TombstoneError, match="NOT_REGULAR"):
        ledger.append(_record(0))


def test_cr02b_n03_pending_binds_reserved_identity() -> None:
    record = _record(0, Phase.PENDING, reserved=(64768, 4242, 24))
    assert (record["reserved_dev"], record["reserved_ino"], record["reserved_size"]) == (64768, 4242, 24)
    assert record["reserved_sha256"] == record["artifact_hash"]
    with pytest.raises(TombstoneError, match="RESERVED_IDENTITY_INVALID"):
        validate_tombstone({**record, "reserved_sha256": "0" * 64})
    with pytest.raises(TombstoneError, match="RESERVED_IDENTITY_INVALID"):
        validate_tombstone({**record, "reserved_ino": -1})


# Windows reports st_dev as a 64-bit volume serial (hosted runners exceed 2**63) and st_ino as a file id of
# up to 128 bits (ReFS). The reserved identity must persist and round-trip; nothing else gets the wider bound.
@pytest.mark.parametrize("reserved", [(2**64 - 1, 2**64 - 1, 24), (2**63 + 5, 2**128 - 1, 24), (0, 0, 0)])
def test_reserved_identity_accepts_windows_filesystem_ids(tmp_path: Path, reserved: tuple[int, int, int]) -> None:
    record = _record(0, Phase.PENDING, reserved=reserved)
    assert (record["reserved_dev"], record["reserved_ino"], record["reserved_size"]) == reserved
    ledger = TombstoneLedger(tmp_path / "t" / "tombstones.jsonl")
    ledger.append(record)
    assert ledger.verify()
    latest = ledger.latest(record["artifact_hash"])
    assert latest is not None and (latest["reserved_dev"], latest["reserved_ino"]) == reserved[:2]


@pytest.mark.parametrize("reserved", [(2**128, 1, 24), (1, 2**128, 24), (-1, 1, 24), (1, 1, 2**63 + 1)])
def test_reserved_identity_out_of_range_fails_closed(reserved: tuple[int, int, int]) -> None:
    with pytest.raises((TombstoneError, DataMinimizationViolation)):
        _record(0, Phase.PENDING, reserved=reserved)
