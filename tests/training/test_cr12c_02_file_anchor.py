"""CR-12c-02: FileExternalAnchor serializes recorders per log name, makes replacements durable, marks
initialization durably (so a vanished head fails closed) and gives every ledger a distinct anchor identity."""
from __future__ import annotations

import hashlib
import multiprocessing
from pathlib import Path

import pytest

from pados.training import _chain
from pados.training._chain import GENESIS, AnchorHead, ChainedLogError, FileExternalAnchor, HashChainedLog
from pados.training.jobs import TrainingJobError, TrainingJobs
from pados.training.registry import RegistryError

from .support import anchor_dir, make_env

FIELDS = ("seq", "v", "prev_hash", "entry_hash")
LOG_ID = "1" * 64
WORKERS = 4
RECORDS_PER_WORKER = 60


def _hash(seq: int) -> str:
    return hashlib.sha256(f"head-{seq}".encode()).hexdigest()


def _record_concurrently(directory: str, worker: int, failures: multiprocessing.Queue) -> None:  # type: ignore[type-arg]
    """One recorder process: records its own increasing heads and checks the anchor never goes back."""
    anchor = FileExternalAnchor(Path(directory))
    anchor.bind("model-registry", LOG_ID)
    seen = 0
    for step in range(RECORDS_PER_WORKER):
        seq = step * WORKERS + worker + 1
        try:
            anchor.record("model-registry", seq, _hash(seq))
        except ChainedLogError as error:  # a higher head was recorded first: monotonic refusal is correct
            if error.code != "LEDGER_ROLLBACK_DETECTED":
                failures.put(f"unexpected {error.code}")
                return
        head = anchor.latest("model-registry")
        if head is None or head.seq < max(seen, seq) or head.head_hash != _hash(head.seq):
            failures.put(f"regressed: saw {seen}, recorded {seq}, now {head}")
            return
        seen = head.seq


def test_concurrent_recorders_never_regress_the_head(tmp_path: Path) -> None:
    directory = tmp_path / "anchor"
    directory.mkdir()
    context = multiprocessing.get_context("spawn")
    failures = context.Queue()
    workers = [context.Process(target=_record_concurrently, args=(str(directory), worker, failures))
               for worker in range(WORKERS)]
    for process in workers:
        process.start()
    for process in workers:
        process.join(timeout=120)
        assert process.exitcode == 0
    problems = []
    while not failures.empty():
        problems.append(failures.get())
    assert problems == []
    top = WORKERS * RECORDS_PER_WORKER
    assert FileExternalAnchor(directory).latest("model-registry") == AnchorHead("model-registry", top, _hash(top))
    assert (directory / ".model-registry.anchor.lock").exists()  # serialized through pados.lifecycle.locking
    assert not list(directory.glob("*.tmp"))


def test_head_replacement_is_followed_by_a_directory_fsync(tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[Path] = []
    monkeypatch.setattr(_chain, "fsync_directory", lambda path: synced.append(Path(path)))
    anchor = FileExternalAnchor(tmp_path)
    anchor.bind("model-registry", LOG_ID)
    synced.clear()
    anchor.record("model-registry", 1, "a" * 64)
    assert synced == [tmp_path]
    anchor.record("model-registry", 1, "a" * 64)  # same head again: nothing written, durability re-established
    assert synced == [tmp_path, tmp_path]


def test_a_head_is_accepted_only_after_a_successful_directory_fsync(tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-12d-03: replacement succeeded but the directory fsync failed. Neither ``latest`` nor an equal-head
    retry may accept that head until the fsync succeeds."""
    anchor = FileExternalAnchor(tmp_path)
    anchor.bind("model-registry", LOG_ID)
    failing = {"on": True}
    real = _chain.fsync_directory

    def flaky(path: object) -> None:
        if failing["on"]:
            raise OSError("fsync failed")
        real(path)  # type: ignore[arg-type]

    monkeypatch.setattr(_chain, "fsync_directory", flaky)
    with pytest.raises(ChainedLogError, match="LEDGER_ANCHOR_UNAVAILABLE"):
        anchor.record("model-registry", 1, "a" * 64)  # the head file was replaced, its durability was not
    for attempt in (lambda: anchor.latest("model-registry"), lambda: anchor.record("model-registry", 1, "a" * 64)):
        with pytest.raises(ChainedLogError, match="LEDGER_ANCHOR_UNAVAILABLE"):
            attempt()
    failing["on"] = False
    anchor.record("model-registry", 1, "a" * 64)  # the retry completes durability
    assert anchor.latest("model-registry") == AnchorHead("model-registry", 1, "a" * 64)


def test_deleting_the_head_after_initialization_fails_closed(tmp_path: Path) -> None:
    anchor = FileExternalAnchor(tmp_path)
    assert anchor.latest("model-registry") is None  # never initialized
    anchor.bind("model-registry", LOG_ID)
    assert anchor.latest("model-registry") == AnchorHead("model-registry", 0, GENESIS)
    anchor.record("model-registry", 3, "b" * 64)
    (tmp_path / "model-registry.anchor.json").unlink()
    for call in (lambda: anchor.latest("model-registry"), lambda: anchor.record("model-registry", 4, "c" * 64),
                 lambda: anchor.bind("model-registry", LOG_ID),
                 lambda: FileExternalAnchor(tmp_path).latest("model-registry")):
        with pytest.raises(ChainedLogError, match="LEDGER_ANCHOR_LOST"):
            call()
    assert not (tmp_path / "model-registry.anchor.json").exists()  # nothing silently re-initialized


def test_head_loss_in_a_live_store_fails_closed_for_every_reader(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.jobs.create(env.contract(), job_id="job-00000001")
    (anchor_dir(tmp_path) / "training-job-events.anchor.json").unlink()
    with pytest.raises(TrainingJobError, match="LEDGER_ANCHOR_LOST"):
        env.jobs.all()
    with pytest.raises(RegistryError, match="LEDGER_ANCHOR_LOST"):
        env.registry.verify_heads()
    with pytest.raises(ChainedLogError, match="LEDGER_ANCHOR_LOST"):  # reopening never re-initializes it
        TrainingJobs(tmp_path / "jobs.jsonl", verifier=env.verifier, registry_log=tmp_path / "registry.jsonl",
                     anchor=FileExternalAnchor(anchor_dir(tmp_path)))


def test_a_head_without_its_initialization_record_is_refused(tmp_path: Path) -> None:
    anchor = FileExternalAnchor(tmp_path)
    anchor.bind("model-registry", LOG_ID)
    anchor.record("model-registry", 1, "a" * 64)
    (tmp_path / "model-registry.anchor.init").unlink()
    with pytest.raises(ChainedLogError, match="LEDGER_ANCHOR_UNAVAILABLE"):
        anchor.latest("model-registry")
    with pytest.raises(ChainedLogError, match="LEDGER_ANCHOR_LOST"):
        anchor.record("model-registry", 2, "b" * 64)


def test_duplicate_log_names_are_refused(tmp_path: Path) -> None:
    directory = tmp_path / "anchor"
    directory.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    anchor = FileExternalAnchor(directory)
    HashChainedLog(store / "a.jsonl", fields=FIELDS, corrupt_code="X", log_name="shared-log", anchor=anchor)
    with pytest.raises(ChainedLogError, match="ANCHOR_LOG_NAME_IN_USE"):  # same anchor object, other log
        HashChainedLog(store / "b.jsonl", fields=FIELDS, corrupt_code="X", log_name="shared-log", anchor=anchor)
    with pytest.raises(ChainedLogError, match="ANCHOR_LOG_NAME_IN_USE"):  # another process: the durable claim
        HashChainedLog(store / "b.jsonl", fields=FIELDS, corrupt_code="X", log_name="shared-log",
                       anchor=FileExternalAnchor(directory))
    reopened = HashChainedLog(store / "a.jsonl", fields=FIELDS, corrupt_code="X", log_name="shared-log",
                              anchor=FileExternalAnchor(directory))  # the same log reopened is fine
    reopened.append(lambda entries: {"v": 1})
    assert reopened.entries()[0]["v"] == 1


def test_training_ledgers_cannot_share_an_anchor_identity(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    for log_name in ("training-decision-ledger", "model-registry"):  # the jobs log posing as another ledger
        with pytest.raises(ChainedLogError, match="ANCHOR_LOG_NAME_IN_USE"):
            TrainingJobs(tmp_path / "jobs.jsonl", verifier=env.verifier, registry_log=tmp_path / "registry.jsonl",
                         anchor=env.anchor, log_name=log_name)
    with pytest.raises(ChainedLogError, match="ANCHOR_LOG_NAME_IN_USE"):  # another job log under the same name
        TrainingJobs(tmp_path / "jobs-2.jsonl", verifier=env.verifier, registry_log=tmp_path / "registry.jsonl",
                     anchor=FileExternalAnchor(anchor_dir(tmp_path)))
