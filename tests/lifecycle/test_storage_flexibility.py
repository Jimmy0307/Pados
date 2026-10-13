"""Storage Flexibility V1: placement policy over replaceable backends; identity, authority and deletion unchanged."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pados.lifecycle.storage_flexibility import (
    ArtifactClass,
    MemoryBackend,
    ObjectStoreBackend,
    PlacementIndex,
    PressureAdvice,
    ReferenceBackend,
    StorageBudget,
    StorageError,
    StorageManager,
    StorageObservation,
    StoragePressure,
    StorageTier,
    choose_tier,
    content_id,
    plan_eviction,
    pressure_of,
    pressure_advice,
)
from pados.state_proof.object_store import ObjectStore

ROOT = Path(__file__).resolve().parents[2]
ROOMY = StorageObservation(total_bytes=1000, free_bytes=900)
TIGHT = StorageObservation(total_bytes=1000, free_bytes=100)  # 10 % free: PRESSURED with the default 15 %
FULL = StorageObservation(total_bytes=1000, free_bytes=20)  # 2 % free: CRITICAL


def manager(tmp_path: Path, **budget: object) -> StorageManager:
    return StorageManager(local=ObjectStoreBackend(ObjectStore(tmp_path / "objects")),
                          external=MemoryBackend("external-disk"), budget=StorageBudget(**budget))  # type: ignore[arg-type]


def test_duplicate_artifacts_are_deduplicated_to_one_physical_copy(tmp_path: Path) -> None:
    m = manager(tmp_path)
    index = m.store(PlacementIndex(), b"same bytes", reference="project-a/report.md",
                    artifact_class=ArtifactClass.PROJECT_ARTIFACT, observation=ROOMY)
    index = m.store(index, b"same bytes", reference="project-b/copy-of-report.md",
                    artifact_class=ArtifactClass.PROJECT_ARTIFACT, observation=ROOMY)
    (entry,) = index.entries.values()
    assert entry.references == ("project-a/report.md", "project-b/copy-of-report.md")
    assert len(entry.placements) == 1
    files = [p for p in (tmp_path / "objects").rglob("*") if p.is_file()]
    assert len(files) == 1 and files[0].name == content_id(b"same bytes")  # the existing ObjectStore layout


def test_large_artifact_is_kept_by_external_reference_not_locally(tmp_path: Path) -> None:
    m = manager(tmp_path, max_local_residency_bytes=10)
    index = m.store(PlacementIndex(), b"x" * 50, reference="dataset.bin", artifact_class=ArtifactClass.LARGE_SOURCE,
                    observation=ROOMY)
    entry = index.resolve("dataset.bin")
    assert entry is not None and entry.placements[0].tier is StorageTier.EXTERNAL_REFERENCE
    assert entry.placements[0].backend_id == "external-disk"
    assert not any(p.is_file() for p in (tmp_path / "objects").rglob("*"))
    assert m.read(index, "dataset.bin", [m.local, m.external]) == b"x" * 50  # type: ignore[list-item]
    no_external = StorageManager(local=m.local, budget=StorageBudget(max_local_residency_bytes=10))
    with pytest.raises(StorageError, match="EXTERNAL_STORAGE_NOT_CONFIGURED"):
        no_external.store(PlacementIndex(), b"y" * 50, reference="big", artifact_class=ArtifactClass.LARGE_SOURCE,
                          observation=ROOMY)


def test_pressure_is_observable_configurable_and_never_guessed() -> None:
    budget = StorageBudget()
    assert pressure_of(ROOMY, budget) is StoragePressure.NORMAL
    assert pressure_of(TIGHT, budget) is StoragePressure.PRESSURED
    assert pressure_of(FULL, budget) is StoragePressure.CRITICAL
    assert pressure_of(None, budget) is StoragePressure.PRESSURED  # unknown: avoid growth, delete nothing
    assert pressure_of(ROOMY, StorageBudget(min_free_bytes=850)) is StoragePressure.CRITICAL  # reserve honoured
    assert pressure_advice(StoragePressure.NORMAL) == ()
    assert PressureAdvice.AVOID_LARGE_LOCAL_DOWNLOADS in pressure_advice(StoragePressure.PRESSURED)
    assert PressureAdvice.PREFER_REMOTE_CAPABILITY_WHERE_PERMITTED in pressure_advice(StoragePressure.CRITICAL)
    with pytest.raises(ValueError, match="STORAGE_PRESSURE_THRESHOLDS_INVALID"):
        StorageBudget(pressured_free_fraction=0.05, critical_free_fraction=0.1)


def test_quota_pressure_refuses_growth_but_never_authoritative_state(tmp_path: Path) -> None:
    m = manager(tmp_path, project_budget_bytes={"p1": 10})
    index = m.store(PlacementIndex(), b"12345678", reference="a", artifact_class=ArtifactClass.PROJECT_ARTIFACT,
                    observation=ROOMY, project_id="p1")
    with pytest.raises(StorageError, match="PROJECT_STORAGE_BUDGET_EXCEEDED"):
        m.store(index, b"abcdefgh", reference="b", artifact_class=ArtifactClass.PROJECT_ARTIFACT, observation=ROOMY,
                project_id="p1")
    with pytest.raises(StorageError, match="STORAGE_CRITICAL_LOCAL_WRITE_REFUSED"):
        m.store(index, b"cache", reference="c", artifact_class=ArtifactClass.REBUILDABLE_CACHE, observation=FULL)
    kept = m.store(index, b'{"receipt": 1}', reference="receipt-1", artifact_class=ArtifactClass.RECEIPT_OR_EVIDENCE,
                   observation=FULL)  # authoritative state is still written locally under critical pressure
    entry = kept.resolve("receipt-1")
    assert entry is not None and entry.placements[0].tier is StorageTier.HOT


def test_authoritative_state_is_never_an_eviction_candidate(tmp_path: Path) -> None:
    m = manager(tmp_path)
    index = PlacementIndex()
    for ref, data, cls in (("receipt", b"r", ArtifactClass.RECEIPT_OR_EVIDENCE),
                           ("profile", b"p", ArtifactClass.AUTHORITATIVE_STATE),
                           ("unique-work", b"w", ArtifactClass.PROJECT_ARTIFACT)):
        index = m.store(index, data, reference=ref, artifact_class=cls, observation=ROOMY)
    plan = plan_eviction(index, m.local.backend_id, StoragePressure.CRITICAL, m.budget)
    assert plan.candidates == ()  # receipts, state and a unique unreplicated artifact: nothing evictable
    assert plan.protected_bytes == 2 and plan.deletes is False and plan.authority == "NONE"
    assert all(p.is_file() for p in (tmp_path / "objects").rglob("*") if p.is_file())


def test_rebuildable_cache_is_evicted_first_and_replicated_artifacts_only_when_critical(tmp_path: Path) -> None:
    m = manager(tmp_path, cache_budget_bytes=2)
    index = m.store(PlacementIndex(), b"cache-bytes", reference="emb", artifact_class=ArtifactClass.REBUILDABLE_CACHE,
                    observation=ROOMY)
    index = m.store(index, b"old-export", reference="export", artifact_class=ArtifactClass.PROJECT_ARTIFACT,
                    observation=ROOMY)
    export = index.resolve("export")
    assert export is not None
    index = m.replicate(index, export.digest, MemoryBackend("nas"))
    normal = plan_eviction(index, m.local.backend_id, StoragePressure.NORMAL, m.budget, cache_bytes_in_use=11)
    assert [c.reason for c in normal.candidates] == ["REBUILDABLE"]  # over the cache budget only
    assert plan_eviction(index, m.local.backend_id, StoragePressure.NORMAL, m.budget).candidates == ()
    pressured = plan_eviction(index, m.local.backend_id, StoragePressure.PRESSURED, m.budget)
    assert [c.reason for c in pressured.candidates] == ["REBUILDABLE"]
    critical = plan_eviction(index, m.local.backend_id, StoragePressure.CRITICAL, m.budget)
    assert sorted(c.reason for c in critical.candidates) == ["REBUILDABLE", "REPLICATED_ELSEWHERE"]


def test_model_weights_are_referenced_never_copied_into_pados_storage(tmp_path: Path) -> None:
    m = manager(tmp_path)
    with pytest.raises(StorageError, match="MODEL_WEIGHTS_ARE_REFERENCED_NOT_STORED"):
        m.store(PlacementIndex(), b"weights", reference="model", artifact_class=ArtifactClass.MODEL_WEIGHTS,
                observation=ROOMY)
    runtime_store = {"a" * 64}
    runtime = ReferenceBackend("model-runtime", probe=lambda d: d in runtime_store)
    index = m.reference_external(PlacementIndex(), digest="a" * 64, size_bytes=5_000_000_000,
                                 reference="ollama-like/model@sha256:aaaa", artifact_class=ArtifactClass.MODEL_WEIGHTS,
                                 backend=runtime, location="runtime-managed")
    entry = index.entry("a" * 64)
    assert entry is not None and entry.placements[0].tier is StorageTier.EXTERNAL_REFERENCE
    assert entry.local_bytes(m.local_backends) == 0
    assert not any(p.is_file() for p in (tmp_path / "objects").rglob("*"))
    with pytest.raises(StorageError, match="REFERENCE_BACKEND_HOLDS_NO_PADOS_BYTES"):
        runtime.put(b"weights")
    with pytest.raises(StorageError, match="MODEL_WEIGHTS_MUST_STAY_WITH_THEIR_RUNTIME"):
        m.reference_external(PlacementIndex(), digest="a" * 64, size_bytes=1, reference="x",
                             artifact_class=ArtifactClass.MODEL_WEIGHTS, backend=MemoryBackend("copy"))
    assert choose_tier(ArtifactClass.MODEL_WEIGHTS, 1, m.budget, StoragePressure.NORMAL) \
        is StorageTier.EXTERNAL_REFERENCE


def test_backends_are_replaceable_and_identity_is_the_same_everywhere(tmp_path: Path) -> None:
    payload = b"portable artifact"
    backends = [ObjectStoreBackend(ObjectStore(tmp_path / "a")), MemoryBackend("s3-compatible"),
                MemoryBackend("nas")]
    digests = {b.put(payload) for b in backends}
    assert digests == {content_id(payload)}  # one immutable identity on every backend
    for backend in backends:
        assert backend.get(content_id(payload)) == payload
    swapped = StorageManager(local=backends[1], external=backends[2])
    index = swapped.store(PlacementIndex(), payload, reference="r", artifact_class=ArtifactClass.PROJECT_ARTIFACT,
                          observation=ROOMY)
    assert index.resolve("r").digest == content_id(payload)  # type: ignore[union-attr]


def test_tampered_or_mislabelled_bytes_are_refused_on_read(tmp_path: Path) -> None:
    good = content_id(b"good")
    liar = ReferenceBackend("archive", probe=lambda d: True, fetch=lambda d: b"evil")
    with pytest.raises(StorageError, match="STORAGE_CONTENT_HASH_MISMATCH"):
        liar.get(good)
    local = ObjectStoreBackend(ObjectStore(tmp_path / "o"))
    digest = local.put(b"good")
    path = tmp_path / "o" / digest[:2] / digest[2:4] / digest
    path.chmod(0o644)
    path.write_bytes(b"tampered")
    with pytest.raises(StorageError, match="STORAGE_CONTENT_HASH_MISMATCH"):
        local.get(digest)
    with pytest.raises(ValueError, match="STORAGE_LOCATION_NOT_A_SAFE_LABEL"):
        from pados.lifecycle.storage_flexibility import Placement

        Placement(backend_id="s3", tier=StorageTier.COLD, location="bucket/key?X-Amz-Signature=abc")


def test_no_second_cas_authority_scheduler_or_deletion_path() -> None:
    source = (ROOT / "src" / "pados" / "lifecycle" / "storage_flexibility.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    forbidden = ("pados.state_proof", "pados.db", "pados.worker", "pados.capability", "threading", "asyncio",
                 "subprocess", "sqlalchemy", "shutil")
    assert not [name for name in imports if name.startswith(forbidden)], imports
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"unlink", "rmtree", "remove", "rmdir", "commit_result", "commit"}
    assert "def commit" not in source and "scope_version" not in source.lower()
