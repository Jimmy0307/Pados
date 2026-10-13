"""The persistence inventory is checked against the real writers (no database)."""
import asyncio
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from pados.compute_node.registry import NodeProber, NodeRegistration, NodeRegistry
from pados.connectivity.state import ConnectivityMonitor, Link
from pados.enterprise.classification import DataClassification
from pados.enterprise.secrets import EnvironmentSecretResolver
from pados.lifecycle.inventory import INVENTORY, Guard, inventory
from pados.lifecycle.minimization import assert_minimized
from pados.lifecycle.retention import CATALOG, DataKind, ProhibitedPersistence
from pados.remote.auth import RemoteActor, RemoteActorRegistry, RemoteSession
from pados.remote.context import RemoteContextReconstructor
from pados.remote.jobs import RemoteJobController
from pados.remote.ledger import IntentLedger
from pados.remote.service import RemoteOperationsService
from pados.remote.staging import StagingArea
from pados.state_proof.object_store import ObjectStore

CONTEXT_MARKER = "LIFECYCLE-CONTEXT-MARKER-7f3a"
SEMANTIC_MARKER = "LIFECYCLE-SEMANTIC-MARKER-91c2"
PROJECT = "proj-lifecycle"


def test_inventory_covers_required_persistence_points() -> None:
    points = inventory()
    assert {"remote_intent_ledger", "remote_staging", "compute_node_probe_state", "connectivity_snapshot",
            "product_object_store", "release_test_evidence", "payload_tombstones"} <= set(points)
    assert len(points) == len(INVENTORY)
    for point in INVENTORY:
        assert point.data_kinds and all(kind in CATALOG for kind in point.data_kinds)
        assert point.may_hold_prohibited == bool(point.risks)
        assert point.categories
    assert points["payload_tombstones"].guard is Guard.ASSERT_MINIMIZED


class _Ports:
    def __init__(self) -> None:
        self.contexts: list[Any] = []

    async def submit_task(self, envelope: Any, context: Any) -> str:
        self.contexts.append(context)
        return "run-1"

    def run_summaries(self, project_ids: Sequence[str]) -> list[dict[str, Any]]:
        return [{"run_id": "run-1", "project_id": PROJECT, "goal": "Refactor billing module", "status": "RUNNING"}]

    def run_summary(self, run_id: str) -> dict[str, Any] | None:
        return None


class _Backend:
    supports_pause = False
    supports_cancel = False

    def exists(self, run_id: str) -> bool:
        return False

    def is_terminal(self, run_id: str) -> bool:
        return False

    def pause(self, run_id: str) -> bool:
        return False

    def resume(self, run_id: str) -> bool:
        return False

    def request_cancel(self, run_id: str) -> bool:
        return False


def _service(tmp_path: Path) -> tuple[RemoteOperationsService, RemoteSession, _Ports]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text(f"Project readme {CONTEXT_MARKER}\n", encoding="utf-8")
    digest = hashlib.sha256(b"owner").hexdigest().upper()
    actor = RemoteActor("owner", digest, frozenset({PROJECT}))
    ports = _Ports()

    def semantic(project_id: str) -> dict[str, Any] | None:
        return {"scope_version": 3, "active_goal": {"item": SEMANTIC_MARKER}} if project_id == PROJECT else None

    service = RemoteOperationsService(
        registry=RemoteActorRegistry([actor]),
        reconstructor=RemoteContextReconstructor(
            semantic_source=semantic, workspace_resolver=lambda pid: workspace if pid == PROJECT else None),
        staging=StagingArea(tmp_path / "staging", project_ceiling=lambda _: DataClassification.CONFIDENTIAL),
        jobs=RemoteJobController(_Backend()),
        ledger=IntentLedger(tmp_path / "ledger" / "intents.jsonl"),
        ports=ports,  # type: ignore[arg-type]
    )
    return service, RemoteSession(actor, "rs-1"), ports


def test_intent_ledger_holds_context_summary_never_context_text(tmp_path: Path) -> None:
    service, session, ports = _service(tmp_path)
    asyncio.run(service.submit(session, {"intent_id": "i-1", "project_id": PROJECT, "intent_type": "START_TASK",
                                         "human_message": "Please do the bounded change"}))
    reconstructed = ports.contexts[0]
    assert any(CONTEXT_MARKER in s.text for s in reconstructed.context.sources)  # the context did contain it
    text = service.ledger.path.read_text(encoding="utf-8")
    assert CONTEXT_MARKER not in text and SEMANTIC_MARKER not in text
    entries = [json.loads(line) for line in text.splitlines()]
    # Cross-process duplicate guard (CR-05): a CLAIMED record precedes the outcome record.
    assert [e["record"].get("claim") for e in entries] == ["CLAIMED", None]
    entry = entries[-1]
    record = entry["record"]
    assert record["context"] == reconstructed.summary()
    assert set(record["context"]) == {"envelope_fingerprint", "context_fingerprint", "scope_id", "scope_version",
                                      "source_count", "context_source", "client_history_included",
                                      "provider_history_included"}
    assert service.ledger.verify()
    # Resolved LEDGER_CONTEXT_FIELD_NAME: the summary holds exactly the reviewed fingerprint fields.
    from pados.remote.context import CONTEXT_SUMMARY_KEYS

    assert set(record["context"]) == CONTEXT_SUMMARY_KEYS
    assert_minimized(record["context"], allowed_keys=CONTEXT_SUMMARY_KEYS)
    assert any(f.startswith("RESOLVED LEDGER_CONTEXT_FIELD_NAME") for f in inventory()["remote_intent_ledger"].findings)


def test_intent_ledger_read_outcomes_do_not_embed_goal_text(tmp_path: Path) -> None:
    service, session, _ = _service(tmp_path)
    asyncio.run(service.submit(session, {"intent_id": "i-2", "project_id": PROJECT, "intent_type": "QUERY_STATUS"}))
    assert "Refactor billing module" not in service.ledger.path.read_text(encoding="utf-8")
    assert any(f.startswith("RESOLVED LEDGER_UNGUARDED") for f in inventory()["remote_intent_ledger"].findings)


def test_staging_record_is_metadata_but_duplicates_are_stored_twice(tmp_path: Path) -> None:
    area = StagingArea(tmp_path / "staging", project_ceiling=lambda _: DataClassification.CONFIDENTIAL)
    content = b"quarterly figures: 42"
    first = area.stage(project_id=PROJECT, actor_id="owner", name="figures.csv", content=content,
                       classification="INTERNAL")
    second = area.stage(project_id=PROJECT, actor_id="owner", name="figures.csv", content=content,
                        classification="INTERNAL")
    record_text = (tmp_path / "staging" / first.staged_id / "record.json").read_text(encoding="utf-8")
    assert "quarterly figures" not in record_text
    assert_minimized(json.loads(record_text))
    blobs = sorted((tmp_path / "staging").glob("stg-*/content.bin"))
    assert len(blobs) == 2 and first.sha256 == second.sha256  # DUPLICATE_UPLOAD finding
    assert ProhibitedPersistence.DUPLICATE_UPLOAD in inventory()["remote_staging"].risks


def test_connectivity_snapshot_is_minimal(tmp_path: Path) -> None:
    path = tmp_path / "conn" / "connectivity.json"
    monitor = ConnectivityMonitor({Link.WAN: lambda: True, Link.REMOTE_TRANSPORT: lambda: False}, snapshot_path=path)
    monitor.observe()
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert_minimized(snapshot, allowed_keys={"links", "observed_at", "home_base", "semantic_authority",
                                             "affects_job_state"})
    assert not inventory()["connectivity_snapshot"].may_hold_prohibited


def test_compute_node_probe_state_never_holds_the_credential(tmp_path: Path) -> None:
    credential = "node-shared-credential-value-5d1e"
    registration = NodeRegistration(node_id="lcn-0123456789abcdef0123", endpoint="http://127.0.0.1:8765",
                                    auth_ref="secretref://env/PADOS_TEST_NODE_KEY", authorized=True)
    registry = NodeRegistry(state_dir=tmp_path / "node-state", nodes=(registration,))

    def http(endpoint: str) -> httpx.Client:
        return httpx.Client(base_url=endpoint, transport=httpx.MockTransport(lambda request: httpx.Response(401)))

    prober = NodeProber(registry, resolver=EnvironmentSecretResolver({"PADOS_TEST_NODE_KEY": credential}), http=http)
    evidence = prober.probe(registration)
    assert evidence["reasons"] == ["NODE_AUTHENTICATION_FAILED"]
    stored = (tmp_path / "node-state" / "probes" / f"{registration.node_id}.json").read_text(encoding="utf-8")
    assert credential not in stored
    assert_minimized(json.loads(stored))
    assert not inventory()["compute_node_probe_state"].may_hold_prohibited


def test_object_store_is_content_addressed_without_duplicates(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "objects")
    first = store.put_bytes(b"artifact", media_type="text/plain", object_kind="RESULT_PAYLOAD")
    second = store.put_bytes(b"artifact", media_type="text/plain", object_kind="RESULT_PAYLOAD")
    assert first.storage_uri == second.storage_uri
    assert len([p for p in (tmp_path / "objects").rglob("*") if p.is_file()]) == 1
    point = inventory()["product_object_store"]
    assert point.guard is Guard.NONE and point.may_hold_prohibited
    assert DataKind.REJECTED_ARTIFACT_PAYLOAD in point.data_kinds


def test_filesystem_id_bound_applies_only_to_declared_paths() -> None:
    from pados.lifecycle.minimization import FILESYSTEM_ID_BOUND, DataMinimizationViolation, assert_minimized

    big = 2**64 - 1
    assert_minimized({"dev": big, "ino": FILESYSTEM_ID_BOUND - 1}, filesystem_id_paths=("dev", "ino"))
    with pytest.raises(DataMinimizationViolation, match="NUMBER_OUT_OF_RANGE"):
        assert_minimized({"dev": big, "count": big}, filesystem_id_paths=("dev",))
    with pytest.raises(DataMinimizationViolation, match="NUMBER_OUT_OF_RANGE"):
        assert_minimized({"dev": FILESYSTEM_ID_BOUND}, filesystem_id_paths=("dev",))
    with pytest.raises(DataMinimizationViolation, match="NUMBER_OUT_OF_RANGE"):
        assert_minimized({"dev": -1}, filesystem_id_paths=("dev",))
