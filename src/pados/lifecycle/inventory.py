# SPDX-License-Identifier: AGPL-3.0-only
"""Inventory of existing PADOS persistence points and their minimization status.

Each entry names the writer, the ``DataKind`` it persists, whether it could
hold prohibited material, and which guard applies today. ``findings`` records
concrete minimization gaps in writers outside the lifecycle package; they are
reported for the owning component and are not fixed here. The inventory is
verified by ``tests/lifecycle/test_minimization_inventory.py`` against the real
writers.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pados.lifecycle.retention import DataKind, ProhibitedPersistence, StorageCategory, policy_for

_P = ProhibitedPersistence


class Guard(StrEnum):
    ASSERT_MINIMIZED = "ASSERT_MINIMIZED"  # pados.lifecycle.minimization with an explicit schema
    STRUCTURAL = "STRUCTURAL"  # fixed, code-built record shape; no assert_minimized call
    NONE = "NONE"  # caller-supplied payload persisted as given


@dataclass(frozen=True)
class PersistencePoint:
    point_id: str
    writer: str
    data_kinds: tuple[DataKind, ...]
    storage: str
    guard: Guard
    may_hold_prohibited: bool
    risks: tuple[ProhibitedPersistence, ...]
    findings: tuple[str, ...] = ()

    @property
    def categories(self) -> tuple[StorageCategory, ...]:
        return tuple(dict.fromkeys(policy_for(kind).category for kind in self.data_kinds))


INVENTORY: tuple[PersistencePoint, ...] = (
    PersistencePoint(
        "remote_intent_ledger", "pados.remote.service.RemoteOperationsService._record -> IntentLedger.append",
        (DataKind.REMOTE_INTENT_LEDGER,), "JSONL hash chain at RemoteConfig.ledger_path", Guard.ASSERT_MINIMIZED,
        may_hold_prohibited=False, risks=(),
        findings=(
            "RESOLVED LEDGER_CONTEXT_FIELD_NAME: the 'context' value is checked against the exact "
            "CONTEXT_SUMMARY_KEYS schema (fingerprints and counts), the rest of the record by assert_minimized.",
            "RESOLVED LEDGER_UNGUARDED: RemoteOperationsService._append guards every record; READ outcomes "
            "ledger run ids plus a digest, never compact() rows (security hardening F-19b).",
            "CLAIM_RECORDS: IntentLedger.claim/release write intent_id, actor, request_fingerprint and claim "
            "state before dispatch (cross-process duplicate guard, CR-05); they pass assert_minimized.",
        ),
    ),
    PersistencePoint(
        "deployment_approval_ledger", "pados.release.approval.ConsumedApprovalLedger.append",
        (DataKind.DEPLOYMENT_APPROVAL_LEDGER,), "operator-chosen JSONL hash chain on the Home Base",
        Guard.ASSERT_MINIMIZED, may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "herdr_telemetry", "pados.herdr.store.HerdrTelemetryStore.record_collection",
        (DataKind.HERDR_TELEMETRY,),
        "PADOS_HERDR_CONFIG store_path (bounded R1 JSON, 0600) + <store>.refkey (0600 digest key)",
        # CR-06 H01/M05: no source-controlled free text is persisted (keyed-digest refs, closed agent
        # kind, operator-bound project/work-package refs, labels never read).
        Guard.ASSERT_MINIMIZED, may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "machine_inventory", "pados.machines.inventory.InventoryStore.save/update (+ pados_known_hosts)",
        (DataKind.MACHINE_INVENTORY,), "PADOS_MACHINES_CONFIG (0600 JSON, cross-process lock)",
        Guard.ASSERT_MINIMIZED, may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "privileged_grant_ledger", "pados.machines.privilege.GrantLedger.append",
        (DataKind.PRIVILEGED_GRANT_LEDGER,), "hash-chained JSONL next to the machine inventory",
        Guard.ASSERT_MINIMIZED, may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "privileged_grant_ledger_head", "pados.machines.privilege.GrantLedger (head file: count, last hash, MAC)",
        (DataKind.PRIVILEGED_GRANT_LEDGER,), "0600 head file next to the grant ledger", Guard.STRUCTURAL,
        may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "machine_credential_policy", "operator-written CredentialPolicy file (pados.machines.policy)",
        (DataKind.MACHINE_INVENTORY,), "owner-only JSON next to the machine inventory", Guard.STRUCTURAL,
        may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "machine_operator_registry", "operator-written registry (pados.machines.operators; SecretRef key refs only)",
        (DataKind.MACHINE_INVENTORY,), "owner-only JSON", Guard.STRUCTURAL, may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "machine_agent_key_files", "pados.machines.ssh pinned agent public keys (agent_keys/*.pub)",
        (DataKind.MACHINE_INVENTORY,), "0600 public-key files next to the inventory", Guard.STRUCTURAL,
        may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "outcome_store", "pados.learning.store.OutcomeStore (append-only JSONL)",
        (DataKind.OUTCOME_RECORD,), "operator-chosen owner-only JSONL", Guard.ASSERT_MINIMIZED,
        may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "outcome_ingestion_ledger", "pados.learning.store.IngestionLedger (<outcomes>.ingest)",
        (DataKind.OUTCOME_RECORD,), "next to the operator-chosen outcome JSONL (owner-only)", Guard.ASSERT_MINIMIZED,
        may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "advice_log_anchor", "pados.learning.anchor.FileAnchor.record (compare-and-set; operator-placed)",
        (DataKind.ADVICE_LOG,), "<operator anchor dir outside the advice log store> (0700 dir, 0600 head/sentinel)",
        Guard.STRUCTURAL, may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "evaluation_store", "pados.learning.store.EvaluationStore",
        (DataKind.EVALUATION_RECORD,), "operator-chosen owner-only JSONL", Guard.ASSERT_MINIMIZED,
        may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "advice_log", "pados.learning.advice_log.AdviceLog (hash-chained JSONL)",
        (DataKind.ADVICE_LOG,), "operator-chosen owner-only JSONL", Guard.ASSERT_MINIMIZED,
        may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "training_job_events", "pados.training.jobs.TrainingJobs via _chain.HashChainedLog.append",
        (DataKind.TRAINING_JOB_EVENT_LOG,), "operator-chosen owner-only JSONL", Guard.ASSERT_MINIMIZED,
        may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "model_registry_ledger", "pados.training.registry.ModelRegistry",
        (DataKind.MODEL_REGISTRY_LEDGER,), "operator-chosen owner-only JSONL", Guard.ASSERT_MINIMIZED,
        may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "training_decision_ledger", "pados.training.decisions.ConsumedDecisionLedger.append",
        (DataKind.TRAINING_DECISION_LEDGER,), "operator-chosen owner-only JSONL", Guard.ASSERT_MINIMIZED,
        may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "capability_execution_ledger",
        "pados.capability.execution_ledger.append_execution (called only by the fabric executor)",
        (DataKind.CAPABILITY_STAGE_EVIDENCE,), "operator-chosen owner-only JSONL (0700 dir, 0600 file)",
        Guard.ASSERT_MINIMIZED, may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "capability_decision_ledger", "pados.capability.decision_ledger.FileConsumedDecisionLedger.consume",
        (DataKind.CAPABILITY_DECISION_LEDGER,), "operator-chosen owner-only JSONL (0700 dir, 0600 file)",
        Guard.ASSERT_MINIMIZED, may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "training_ledger_anchor", "pados.training._chain.FileExternalAnchor.record (optional, operator-placed)",
        (DataKind.TRAINING_DECISION_LEDGER, DataKind.MODEL_REGISTRY_LEDGER, DataKind.TRAINING_JOB_EVENT_LOG),
        "<operator anchor dir outside the log store>/<log_name>.anchor.json (+ .anchor.init)", Guard.ASSERT_MINIMIZED,
        may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "remote_staging", "pados.remote.staging.StagingArea.stage (content.bin + record.json)",
        (DataKind.TEMPORARY_UPLOAD, DataKind.QUARANTINED_UPLOAD), "RemoteConfig.staging_root/stg-<id>/",
        Guard.STRUCTURAL, may_hold_prohibited=True, risks=(_P.DUPLICATE_UPLOAD,),
        findings=(
            "STAGING_DUPLICATE_UPLOAD: identical bytes staged twice are stored twice (no content-hash dedupe); "
            "record.json holds metadata only (sha256, size, classification, original file name).",
        ),
    ),
    PersistencePoint(
        "compute_node_probe_state", "pados.compute_node.registry.NodeProber._store",
        (DataKind.MACHINE_CONNECTION_HISTORY,), "NodeRegistry.state_dir/probes/<node_id>.json",
        Guard.STRUCTURAL, may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "connectivity_snapshot", "pados.connectivity.state.ConnectivityMonitor.observe",
        (DataKind.CONNECTIVITY_HISTORY,), "ConnectivityConfig.snapshot_path", Guard.STRUCTURAL,
        may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "product_object_store", "pados.state_proof.object_store.ObjectStore.put_json/put_bytes",
        (DataKind.ACTIVE_ARTIFACT, DataKind.REJECTED_ARTIFACT_PAYLOAD),
        "PADOS_PRODUCT_OBJECT_ROOT/<h[:2]>/<h[2:4]>/<sha256>", Guard.NONE,
        may_hold_prohibited=True, risks=(_P.DUPLICATED_PROVIDER_CONTEXT,),
        findings=(
            "OBJECT_STORE_RUN_CONTEXT: ProductLoopService._run_context_payload persists the caller's "
            "request.context as 'bounded_context' in a content-addressed run-context object; it is bounded "
            "by the request schema but not passed through assert_minimized.",
        ),
    ),
    PersistencePoint(
        "capability_artifact_store", "pados.capability.artifacts.ObjectStoreArtifacts.put (via ScopedArtifacts)",
        (DataKind.DERIVED_DOCUMENT, DataKind.CAPABILITY_INTERMEDIATE_ARTIFACT, DataKind.CAPABILITY_STAGE_EVIDENCE),
        "operator-chosen root/<h[:2]>/<h[2:4]>/<sha256>", Guard.NONE, may_hold_prohibited=False, risks=(),
        findings=(
            "CAPABILITY_ARTIFACT_CONTENT: stage output bytes (e.g. OCR text) are project content written as "
            "given; only StageOutput evidence is credential-checked. Retention follows the source lineage.",
        ),
    ),
    PersistencePoint(
        "release_test_evidence", "release / acceptance gate runners writing runtime/artifacts/reports/",
        (DataKind.RELEASE_TEST_EVIDENCE,), "runtime/artifacts/reports/", Guard.NONE,
        may_hold_prohibited=True, risks=(_P.FULL_TERMINAL_HISTORY,),
        findings=("EVIDENCE_COMMAND_LOGS: acceptance logs capture command output and are R2 evidence; review "
                  "before release, never purged automatically.",),
    ),
    PersistencePoint(
        "payload_tombstones", "pados.lifecycle.tombstones.TombstoneLedger.append",
        (DataKind.PAYLOAD_TOMBSTONE,), "operator-chosen JSONL hash chain", Guard.ASSERT_MINIMIZED,
        may_hold_prohibited=False, risks=(),
    ),
    PersistencePoint(
        "storage_placement_index", "pados.lifecycle.storage_index.PlacementIndexStore.save",
        (DataKind.STORAGE_PLACEMENT_INDEX,),
        "operator-chosen owner-only JSON, digest-sealed, atomic replace under a store lock", Guard.ASSERT_MINIMIZED,
        may_hold_prohibited=False, risks=(),
        findings=(
            "PLACEMENT_INDEX_CONTENT_FREE: entries hold digests, sizes, classes, tiers, backend ids and safe "
            "labels only; references and locations must be safe labels (no paths with query strings, no "
            "credentials). The embedded digest detects accidental or naive tampering; callers that need "
            "stronger tamper evidence pass the expected digest from an anchor they keep elsewhere.",
        ),
    ),
)


def inventory() -> dict[str, PersistencePoint]:
    return {point.point_id: point for point in INVENTORY}
