"""Storage Flexibility V1+: configured policy, retention mapping, persisted index, reporting, pressure snapshot."""
from __future__ import annotations

import ast
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from pados.config import Settings
from pados.lifecycle.accounting import account_storage, build_locations
from pados.lifecycle.inventory import Guard, inventory
from pados.lifecycle.purge import PurgeError, PurgeScope, plan_purge, scopes_from_locations
from pados.lifecycle.retention import CATALOG, DataKind, PurgeRule, RetentionClass
from pados.lifecycle.storage_flexibility import (
    ArtifactClass,
    MemoryBackend,
    ObjectStoreBackend,
    PlacementIndex,
    ReferenceBackend,
    StorageBudget,
    StorageError,
    StorageManager,
    StorageObservation,
    StoragePressure,
    StoragePressureSnapshot,
    StorageTier,
    content_id,
    pressure_of,
    pressure_snapshot,
)
from pados.lifecycle.storage_index import PlacementIndexError, PlacementIndexStore, index_digest
from pados.lifecycle.storage_policy import (
    StoragePolicy,
    StoragePolicyError,
    load_storage_policy,
    storage_policy_from_document,
)
from pados.lifecycle.storage_report import (
    PLACEMENT_LOCATION_ID,
    class_retention_table,
    placement_location,
    placement_report,
    recommend_eviction,
)
from pados.state_proof.object_store import ObjectStore

ROOT = Path(__file__).resolve().parents[2]
ROOMY = StorageObservation(total_bytes=1000, free_bytes=900)
FULL = StorageObservation(total_bytes=1000, free_bytes=20)
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
SCHEMA = "pados.lifecycle.storage-policy.v1"


def manager(tmp_path: Path, **budget: object) -> StorageManager:
    return StorageManager(local=ObjectStoreBackend(ObjectStore(tmp_path / "objects")),
                          external=MemoryBackend("external-disk"), budget=StorageBudget(**budget))  # type: ignore[arg-type]


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- 1. configured policy, fail closed ----------------------------------------------------------------------------

def test_policy_loads_from_json_and_toml_with_no_hard_coded_limits(tmp_path: Path) -> None:
    default = load_storage_policy("")
    assert default == StoragePolicy() and load_storage_policy(None) == default
    b = default.budget
    assert (b.cache_budget_bytes, b.max_local_residency_bytes, b.local_budget_bytes, dict(b.project_budget_bytes),
            b.min_free_bytes) == (None, None, None, {}, 0)  # no size limit unless configured
    document = {"schema_version": SCHEMA,
                "budget": {"min_free_bytes": 5, "local_budget_bytes": 4096, "cache_budget_bytes": 512,
                           "max_local_residency_bytes": 1024, "project_budget_bytes": {"p1": 2048}},
                "retention": {"r0_hours": 12, "r1_days": 21},
                "class_data_kinds": {"RECEIPT_OR_EVIDENCE": "PROJECT_EVIDENCE"}}
    from_json = load_storage_policy(write(tmp_path / "storage.json", json.dumps(document)))
    toml = """schema_version = "pados.lifecycle.storage-policy.v1"
class_data_kinds = { RECEIPT_OR_EVIDENCE = "PROJECT_EVIDENCE" }
[budget]
min_free_bytes = 5
local_budget_bytes = 4096
cache_budget_bytes = 512
max_local_residency_bytes = 1024
project_budget_bytes = { p1 = 2048 }
[retention]
r0_hours = 12
r1_days = 21
"""
    from_toml = load_storage_policy(write(tmp_path / "storage.toml", toml))
    assert from_json == from_toml
    assert from_json.retention_policy.r1 == timedelta(days=21)
    assert from_json.retention_class(ArtifactClass.RECEIPT_OR_EVIDENCE) is RetentionClass.R2_PROJECT


@pytest.mark.parametrize(("document", "code"), [
    ({"schema_version": "pados.lifecycle.storage-policy.v0"}, "STORAGE_POLICY_SCHEMA_UNSUPPORTED"),
    ({}, "STORAGE_POLICY_SCHEMA_UNSUPPORTED"),
    ({"schema_version": SCHEMA, "surprise": 1}, "STORAGE_POLICY_INVALID"),
    ({"schema_version": SCHEMA, "budget": {"cache_budget_bytes": "512"}}, "STORAGE_POLICY_INVALID"),
    ({"schema_version": SCHEMA, "budget": {"local_budget_bytes": True}}, "STORAGE_POLICY_INVALID"),
    ({"schema_version": SCHEMA, "budget": {"min_free_bytes": -1}}, "STORAGE_POLICY_INVALID"),
    ({"schema_version": SCHEMA, "budget": {"disk_size": 10}}, "STORAGE_POLICY_INVALID"),
    ({"schema_version": SCHEMA, "budget": {"pressured_free_fraction": 0.01}}, "STORAGE_POLICY_INVALID"),
    ({"schema_version": SCHEMA, "budget": {"project_budget_bytes": {"../escape": 1}}}, "STORAGE_POLICY_INVALID"),
    ({"schema_version": SCHEMA, "retention": {"r1_days": 365}}, "STORAGE_POLICY_INVALID"),  # needs override
    ({"schema_version": SCHEMA, "class_data_kinds": {"RECEIPT_OR_EVIDENCE": "OPERATIONAL_LOG"}},
     "STORAGE_POLICY_INVALID"),  # receipts may never expire by age
    ({"schema_version": SCHEMA, "class_data_kinds": {"AUTHORITATIVE_STATE": "CACHE_ENTRY"}}, "STORAGE_POLICY_INVALID"),
    ({"schema_version": SCHEMA, "class_data_kinds": {"REBUILDABLE_CACHE": "ACTIVE_ARTIFACT"}},
     "STORAGE_POLICY_INVALID"),  # EPHEMERAL is R0 only
    ({"schema_version": SCHEMA, "class_data_kinds": {"PROJECT_ARTIFACT": "REJECTED_ARTIFACT_PAYLOAD"}},
     "STORAGE_POLICY_INVALID"),
    ({"schema_version": SCHEMA, "class_data_kinds": {"MODEL_WEIGHTS": "ACTIVE_ARTIFACT"}}, "STORAGE_POLICY_INVALID"),
    ({"schema_version": SCHEMA, "class_data_kinds": {"NOT_A_CLASS": "ACTIVE_ARTIFACT"}}, "STORAGE_POLICY_INVALID"),
    ([], "STORAGE_POLICY_NOT_AN_OBJECT"),
])
def test_invalid_policy_documents_are_refused(document: object, code: str) -> None:
    with pytest.raises(StoragePolicyError) as caught:
        storage_policy_from_document(document)
    assert caught.value.code == code


def test_invalid_policy_files_are_refused(tmp_path: Path) -> None:
    cases = {
        "dup.json": ('{"schema_version": "%s", "budget": {}, "budget": {}}' % SCHEMA, "STORAGE_POLICY_DUPLICATE_KEY"),
        "nan.json": ('{"schema_version": "%s", "budget": {"pressured_free_fraction": NaN}}' % SCHEMA,
                     "STORAGE_POLICY_NON_FINITE_NUMBER"),
        "broken.json": ("{", "STORAGE_POLICY_UNPARSEABLE"),
        "broken.toml": ("schema_version = ", "STORAGE_POLICY_UNPARSEABLE"),
        "policy.yaml": ("schema_version: x", "STORAGE_POLICY_FORMAT_UNSUPPORTED"),
    }
    for name, (text, code) in cases.items():
        with pytest.raises(StoragePolicyError) as caught:
            load_storage_policy(write(tmp_path / name, text))
        assert caught.value.code == code, name
    with pytest.raises(StoragePolicyError, match="STORAGE_POLICY_NOT_A_REGULAR_FILE"):
        load_storage_policy(tmp_path / "missing.json")  # configured but absent: refuse, never default
    with pytest.raises(StoragePolicyError, match="STORAGE_POLICY_NOT_A_REGULAR_FILE"):
        load_storage_policy(tmp_path)


# --- 2. tier / class -> existing retention vocabulary --------------------------------------------------------------

def test_classes_map_onto_the_existing_retention_vocabulary() -> None:
    policy = StoragePolicy()
    table = class_retention_table(policy)
    assert table == {"AUTHORITATIVE_STATE": "R3_AUTHORITY", "RECEIPT_OR_EVIDENCE": "R3_AUTHORITY",
                     "PROJECT_ARTIFACT": "R2_PROJECT", "LARGE_SOURCE": "R2_PROJECT", "MODEL_WEIGHTS": "R3_AUTHORITY",
                     "REBUILDABLE_CACHE": "R0_EPHEMERAL", "INTERMEDIATE": "R0_EPHEMERAL"}
    assert set(table.values()) <= {c.value for c in RetentionClass}  # no second vocabulary
    for cls in ArtifactClass:
        assert policy.data_kind(cls) in CATALOG
        if cls.protected or cls in {ArtifactClass.PROJECT_ARTIFACT, ArtifactClass.LARGE_SOURCE}:
            assert not policy.expires_by_age(cls)  # receipts, authority and unique work never age out
    assert policy.tier_retention(StorageTier.EPHEMERAL, ArtifactClass.REBUILDABLE_CACHE) is RetentionClass.R0_EPHEMERAL
    with pytest.raises(StoragePolicyError, match="STORAGE_EPHEMERAL_TIER_IS_R0_ONLY"):
        policy.tier_retention(StorageTier.EPHEMERAL, ArtifactClass.RECEIPT_OR_EVIDENCE)


# --- 3. persisted placement index ------------------------------------------------------------------------------------

def populated(tmp_path: Path) -> tuple[StorageManager, PlacementIndex]:
    m = manager(tmp_path, max_local_residency_bytes=64)
    index = m.store(PlacementIndex(), b"report bytes", reference="project-a/report.md",
                    artifact_class=ArtifactClass.PROJECT_ARTIFACT, observation=ROOMY, project_id="project-a")
    index = m.store(index, b"report bytes", reference="project-b/report-copy.md",
                    artifact_class=ArtifactClass.PROJECT_ARTIFACT, observation=ROOMY)
    index = m.store(index, b"r" * 200, reference="dataset.bin", artifact_class=ArtifactClass.LARGE_SOURCE,
                    observation=ROOMY)
    index = m.store(index, b'{"receipt": 1}', reference="receipt-1", artifact_class=ArtifactClass.RECEIPT_OR_EVIDENCE,
                    observation=ROOMY)
    index = m.store(index, b"embedding cache", reference="cache/emb-1", artifact_class=ArtifactClass.REBUILDABLE_CACHE,
                    observation=ROOMY)
    runtime = ReferenceBackend("model-runtime", probe=lambda d: d == "a" * 64)
    index = m.reference_external(index, digest="a" * 64, size_bytes=4_000_000_000, reference="runtime/model-x@v1",
                                 artifact_class=ArtifactClass.MODEL_WEIGHTS, backend=runtime, location="runtime-managed")
    return m, index


def test_index_round_trips_through_the_registered_persistence_point(tmp_path: Path) -> None:
    _, index = populated(tmp_path)
    store = PlacementIndexStore(tmp_path / "state" / "placement-index.json")
    assert store.load(fresh_store=True) == PlacementIndex()  # missing file of a fresh store: empty index
    digest = store.save(index)
    assert digest == index_digest(index)
    assert store.load() == index and store.load(expected_digest=digest) == index
    text = store.path.read_text(encoding="utf-8")
    assert "report bytes" not in text and "receipt" in text  # labels only, never the bytes
    if os.name != "nt":
        assert store.path.stat().st_mode & 0o077 == 0  # owner-only
    point = inventory()["storage_placement_index"]
    assert point.data_kinds == (DataKind.STORAGE_PLACEMENT_INDEX,) and point.guard is Guard.ASSERT_MINIMIZED
    policy = CATALOG[DataKind.STORAGE_PLACEMENT_INDEX]
    assert policy.retention is RetentionClass.R2_PROJECT and policy.purge_rule is PurgeRule.HUMAN_DECISION_ONLY
    assert not policy.authoritative  # placement bookkeeping is not authority


def test_tampered_or_unexpected_index_is_refused(tmp_path: Path) -> None:
    _, index = populated(tmp_path)
    store = PlacementIndexStore(tmp_path / "placement-index.json")
    digest = store.save(index)
    document = json.loads(store.path.read_text(encoding="utf-8"))
    document["entries"][0]["size_bytes"] += 1
    store.path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(PlacementIndexError, match="STORAGE_INDEX_TAMPERED"):
        store.load()
    # A rewrite that also recomputes the embedded digest is caught by the digest the caller kept elsewhere.
    from pados.execution.context import canonical_hash

    document["index_digest"] = canonical_hash({"schema_version": document["schema_version"],
                                               "entries": document["entries"]})
    store.path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(PlacementIndexError, match="STORAGE_INDEX_NOT_THE_EXPECTED_VERSION"):
        store.load(expected_digest=digest)
    for mutate, code in ((lambda d: d.update(extra=1), "STORAGE_INDEX_SHAPE_INVALID"),
                         (lambda d: d.update(schema_version="v0"), "STORAGE_INDEX_SCHEMA_UNSUPPORTED"),
                         (lambda d: d.update(index_digest="x"), "STORAGE_INDEX_DIGEST_INVALID")):
        bad = json.loads(json.dumps(document))
        mutate(bad)
        store.path.write_text(json.dumps(bad), encoding="utf-8")
        with pytest.raises(PlacementIndexError, match=code):
            store.load()
    store.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(PlacementIndexError, match="STORAGE_INDEX_UNREADABLE"):
        store.load()
    with pytest.raises(PlacementIndexError, match="STORAGE_INDEX_MISSING"):
        PlacementIndexStore(tmp_path / "absent.json").load(expected_digest=digest)


def test_a_deleted_index_is_refused_unless_the_caller_says_the_store_is_fresh(tmp_path: Path) -> None:
    """Red-team F-11: deleting the index file no longer silently yields an empty index."""
    _, index = populated(tmp_path)
    store = PlacementIndexStore(tmp_path / "placement-index.json")
    digest = store.save(index)
    store.path.unlink()
    for kwargs in ({}, {"fresh_store": False}, {"expected_digest": digest},
                   {"expected_digest": digest, "fresh_store": True}, {"fresh_store": 1}):
        with pytest.raises(PlacementIndexError, match="STORAGE_INDEX_MISSING"):
            store.load(**kwargs)  # type: ignore[arg-type]
    assert store.load(fresh_store=True) == PlacementIndex()


def test_index_refuses_non_label_references_and_credential_locations(tmp_path: Path) -> None:
    m = manager(tmp_path)
    index = m.store(PlacementIndex(), b"x", reference="free text with spaces", artifact_class=ArtifactClass.INTERMEDIATE,
                    observation=ROOMY)
    with pytest.raises(PlacementIndexError, match="STORAGE_INDEX_LABEL_NOT_SAFE"):
        PlacementIndexStore(tmp_path / "i.json").save(index)
    assert not (tmp_path / "i.json").exists()
    entry = next(iter(index.entries.values())).model_dump(mode="json")
    entry["placements"][0]["location"] = "user:pw@host/bucket?sig=1"
    body = {"schema_version": "pados.lifecycle.placement-index.v1", "entries": [entry]}
    from pados.execution.context import canonical_hash

    (tmp_path / "j.json").write_text(json.dumps({**body, "index_digest": canonical_hash(body)}), encoding="utf-8")
    with pytest.raises(PlacementIndexError, match="STORAGE_INDEX_ENTRY_INVALID"):
        PlacementIndexStore(tmp_path / "j.json").load()


# --- 4. accounting integration and eviction recommendation ----------------------------------------------------------

def test_report_by_tier_class_backend_counts_dedup_and_references(tmp_path: Path) -> None:
    m, index = populated(tmp_path)
    report = placement_report(index, local_backends=m.local_backends)
    assert report["recommendation_only"] and not report["autonomous_deletion"] and report["authority"] == "NONE"
    totals = report["totals"]
    assert totals["deduplication_saved_bytes"] == len(b"report bytes")  # two references, one physical copy
    assert totals["referenced_only_bytes"] == 200 + 4_000_000_000  # large source and weights are not local
    assert totals["local_bytes"] == index.local_bytes(m.local_backends)
    assert report["by_tier"]["EXTERNAL_REFERENCE"]["artifacts"] == 2
    assert report["by_backend"]["model-runtime"]["local_bytes"] == 0
    assert report["by_retention"]["R0_EPHEMERAL"]["artifacts"] == 1
    text = json.dumps(report)
    assert "report.md" not in text and "dataset.bin" not in text and "runtime-managed" not in text


def test_index_is_an_accounted_protected_location(tmp_path: Path) -> None:
    _, index = populated(tmp_path)
    PlacementIndexStore(tmp_path / "state" / "placement-index.json").save(index)
    location = placement_location(tmp_path / "state" / "placement-index.json")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    locations = build_locations(settings, runtime_root=tmp_path / "runtime", extra=[location])
    report = account_storage(locations, now=NOW)
    usage = next(u for u in report.locations if u.location_id == PLACEMENT_LOCATION_ID)
    assert usage.bytes > 0 and usage.eligible_for_purge.bytes == 0
    assert "NOT_ELIGIBLE_R2_HUMAN_DECISION_ONLY" in usage.eligible_for_purge.basis
    scopes, protected = scopes_from_locations(locations)
    assert PLACEMENT_LOCATION_ID not in {s.scope_id for s in scopes} and location.root in protected


def test_eviction_recommendation_composes_with_purge_and_never_deletes(tmp_path: Path) -> None:
    m, index = populated(tmp_path)
    work = index.resolve("project-a/report.md")
    assert work is not None
    index = m.replicate(index, work.digest, MemoryBackend("nas"))
    files_before = sorted(p.name for p in (tmp_path / "objects").rglob("*") if p.is_file())
    critical = recommend_eviction(index, m.local.backend_id, StoragePressure.CRITICAL)
    assert critical.deletes is False and critical.authority == "NONE" and critical.recommendation_only
    routes = {r.reason: r for r in critical.routes}
    assert set(routes) == {"REBUILDABLE", "REPLICATED_ELSEWHERE"}
    cache, replicated = routes["REBUILDABLE"], routes["REPLICATED_ELSEWHERE"]
    assert (cache.data_kind, cache.retention, cache.route) == (DataKind.CACHE_ENTRY, RetentionClass.R0_EPHEMERAL,
                                                               "PLAN_PURGE_R0_R1_SCOPE")
    assert (replicated.retention, replicated.route) == (RetentionClass.R2_PROJECT, "HUMAN_DECISION_ONLY")
    assert all(r.retention is not RetentionClass.R3_AUTHORITY for r in critical.routes)  # receipts never listed
    # The routes agree with the one deletion path: an R0 cache scope is plannable, an R2 scope is refused.
    cache_root = tmp_path / "cache-scope"
    cache_root.mkdir()
    old = cache_root / "emb-1.bin"
    old.write_bytes(b"embedding cache")
    stale = (NOW - timedelta(days=3)).timestamp()
    os.utime(old, (stale, stale))
    plan = plan_purge([PurgeScope("cache_scope", cache_root, cache.data_kind)], now=NOW)
    assert [i.relpath for i in plan.items] == ["emb-1.bin"]
    with pytest.raises(PurgeError, match="PURGE_SCOPE_NOT_PURGEABLE"):
        plan_purge([PurgeScope("work_scope", cache_root, replicated.data_kind)], now=NOW)
    assert old.exists()  # planning deleted nothing
    assert sorted(p.name for p in (tmp_path / "objects").rglob("*") if p.is_file()) == files_before


# --- 5/6. quota pressure, authoritative state, pressure snapshot ---------------------------------------------------

def test_local_budget_pressure_refuses_growth_but_writes_authoritative_state(tmp_path: Path) -> None:
    m = manager(tmp_path, local_budget_bytes=100)
    index = m.store(PlacementIndex(), b"a" * 60, reference="w1", artifact_class=ArtifactClass.PROJECT_ARTIFACT,
                    observation=ROOMY)
    with pytest.raises(StorageError, match="LOCAL_STORAGE_BUDGET_EXCEEDED"):
        m.store(index, b"b" * 50, reference="w2", artifact_class=ArtifactClass.PROJECT_ARTIFACT, observation=ROOMY)
    assert pressure_of(ROOMY, m.budget, 60) is StoragePressure.NORMAL
    assert pressure_of(ROOMY, m.budget, 90) is StoragePressure.PRESSURED
    assert pressure_of(ROOMY, m.budget, 97) is StoragePressure.CRITICAL
    index = m.store(index, b"c" * 39, reference="w3", artifact_class=ArtifactClass.PROJECT_ARTIFACT,
                    observation=ROOMY)  # 99 of 100 bytes
    with pytest.raises(StorageError, match="STORAGE_CRITICAL_LOCAL_WRITE_REFUSED"):
        m.store(index, b"d", reference="cache", artifact_class=ArtifactClass.REBUILDABLE_CACHE, observation=ROOMY)
    kept = m.store(index, b'{"state": 1}', reference="state-1", artifact_class=ArtifactClass.AUTHORITATIVE_STATE,
                   observation=FULL)  # over budget and CRITICAL disk: authoritative state is still written
    entry = kept.resolve("state-1")
    assert entry is not None and entry.placements[0].tier is StorageTier.HOT
    plan = recommend_eviction(kept, m.local.backend_id, StoragePressure.CRITICAL)
    assert entry.digest not in {r.digest for r in plan.routes}


def test_pressure_snapshot_is_advisory_and_changes_nothing(tmp_path: Path) -> None:
    m, index = populated(tmp_path)
    before = index_digest(index)
    snapshot = pressure_snapshot(FULL, m.budget, index=index, local_backends=m.local_backends)
    assert snapshot.pressure is StoragePressure.CRITICAL and snapshot.advice
    assert (snapshot.authority, snapshot.affects_authorization, snapshot.affects_semantic_state,
            snapshot.recommendation_only) == ("NONE", False, False, True)
    assert index_digest(index) == before  # nothing in the index moved
    assert snapshot == pressure_snapshot(FULL, m.budget, index=index, local_backends=m.local_backends)  # pure
    unknown = pressure_snapshot(None, m.budget)
    assert unknown.pressure is StoragePressure.PRESSURED and not unknown.observed and unknown.local_bytes is None
    for field, value in (("authority", "ROUTER"), ("affects_authorization", True), ("affects_semantic_state", True)):
        with pytest.raises(ValidationError):
            StoragePressureSnapshot(pressure=StoragePressure.NORMAL, advice=(), observed=True, **{field: value})
    with pytest.raises(ValidationError):
        snapshot.model_copy(update={}).__setattr__("pressure", StoragePressure.NORMAL)  # frozen


# --- 7. replaceability and identity ----------------------------------------------------------------------------------

def test_backend_can_be_replaced_and_the_persisted_identity_holds(tmp_path: Path) -> None:
    m, index = populated(tmp_path)
    work = index.resolve("project-a/report.md")
    assert work is not None
    nas = MemoryBackend("nas")
    index = m.replicate(index, work.digest, nas)
    store = PlacementIndexStore(tmp_path / "placement-index.json")
    store.save(index)
    reloaded = store.load()
    replacement = StorageManager(local=MemoryBackend("new-local"), external=nas)
    assert replacement.read(reloaded, "project-b/report-copy.md", [nas]) == b"report bytes"  # old local gone
    archive = ReferenceBackend("archive", probe=lambda d: True, fetch=lambda d: b"report bytes")
    adapters = (m.local, nas, archive)
    assert {content_id(a.get(work.digest)) for a in adapters} == {work.digest}  # same identity on every adapter
    assert reloaded.resolve("project-a/report.md").digest == work.digest  # type: ignore[union-attr]


def test_model_weights_stay_with_the_runtime_in_the_policy_and_index(tmp_path: Path) -> None:
    m, index = populated(tmp_path)
    weights = index.entry("a" * 64)
    assert weights is not None and weights.local_bytes(m.local_backends) == 0
    assert StoragePolicy().retention_class(ArtifactClass.MODEL_WEIGHTS) is RetentionClass.R3_AUTHORITY  # identity
    with pytest.raises(StorageError, match="MODEL_WEIGHTS_ARE_REFERENCED_NOT_STORED"):
        m.store(index, b"weights", reference="w", artifact_class=ArtifactClass.MODEL_WEIGHTS, observation=ROOMY)
    assert not any(p.stat().st_size >= 4_000_000_000 for p in (tmp_path / "objects").rglob("*") if p.is_file())


# --- static: no second CAS, no deletion, no authority -----------------------------------------------------------------

@pytest.mark.parametrize("name", ["storage_flexibility.py", "storage_policy.py", "storage_index.py",
                                  "storage_report.py"])
def test_no_second_cas_no_deletion_and_no_authority(name: str) -> None:
    source = (ROOT / "src" / "pados" / "lifecycle" / name).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    forbidden = ("pados.state_proof", "pados.db", "pados.worker", "pados.capability", "pados.resources",
                 "pados.lifecycle.purge", "pados.lifecycle.tombstones", "threading", "asyncio", "subprocess",
                 "shutil")
    assert not [i for i in imports if i.startswith(forbidden)], imports
    calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not calls & {"unlink", "rmtree", "remove", "rmdir", "commit_result", "commit", "apply_purge", "put_json"}
    assert "scope_version" not in source.lower()
