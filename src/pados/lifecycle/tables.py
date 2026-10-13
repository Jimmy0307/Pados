# SPDX-License-Identifier: AGPL-3.0-only
"""Database table -> storage category / data kind map (from ``migrations/``).

Every table created by the operational lane (``migrations/versions``) and the
proof lane (``migrations/p6_19b/versions``) is listed once. The map is used for
size accounting only: it never authorizes deleting a row. Mapping a table to an
R0/R1 kind describes its nature; DB row purge is not part of Lifecycle V1.
"""
from __future__ import annotations

from pados.lifecycle.retention import DataKind, StorageCategory, policy_for

_K = DataKind

_BY_KIND: dict[DataKind, tuple[str, ...]] = {
    # R3 authority metadata
    _K.AUDIT_CHAIN: ("audit_events", "state_proof_semantic_audit", "state_proof_semantic_audit_heads",
                     "memory_authority_events"),
    _K.CAS_TRANSITION: ("state_proof_commits",),
    _K.SCOPE_VERSION_TRANSITION: ("state_proof_scope_heads", "state_proof_scope_versions"),
    _K.RESULT_RECEIPT_SUMMARY: ("state_proof_results",),
    _K.ARTIFACT_HASH: ("state_proof_objects",),
    _K.DECISION_IDENTITY: ("memory_decisions", "development_decisions", "architecture_decisions",
                           "control_approvals", "project_human_gates", "capability_changes",
                           "residual_plan_amendments", "approval_step_up_challenges",
                           "result_acceptance_decisions"),
    _K.ACCEPTANCE_STATUS: ("development_validation_receipts", "capability_versions", "capability_registry",
                           "result_acceptance_candidates"),
    # R2 project content and evidence
    _K.GOVERNED_PROJECT_CONTENT: (
        "projects", "context_project_registry", "memory_records", "memory_relationships",
        "graph_workspaces", "graph_nodes", "graph_edges", "graph_groups", "graph_intents",
        "cognitive_work_items", "project_goals", "project_plans", "project_tasks",
        "project_task_dependencies", "project_gaps", "project_replan_proposals", "tasks", "task_goals",
        "task_plans", "task_plan_steps", "task_dependencies", "skills", "goal_runtime_bindings",
    ),
    _K.PROJECT_SOURCE: (
        "file_registry", "file_chunks", "context_sources", "corpus_sources", "corpus_documents",
        "corpus_document_versions", "corpus_pages", "corpus_chunks", "corpus_evidence_spans",
        "corpus_document_relationships", "wiki_pages", "wiki_sources",
    ),
    _K.ACTIVE_ARTIFACT: ("artifacts", "worker_job_artifacts"),
    _K.PROJECT_EVIDENCE: ("memory_artifacts", "development_artifacts", "development_change_sets",
                          "development_change_items", "development_backfill_records",
                          "capability_delta_candidates", "checkpoints"),
    _K.RELEASE_TEST_EVIDENCE: ("test_runs", "review_findings"),
    # R1 operational records
    _K.OPERATIONAL_LOG: (
        "run_events", "agent_runs", "agent_handoffs", "task_loops", "task_loop_events",
        "task_loop_iterations", "task_loop_leases", "project_controller_leases", "worker_jobs",
        "worker_job_events", "control_messages", "control_sync_events", "control_work_requests",
        "development_events", "development_sessions", "corpus_ingestion_batches",
        "corpus_ingestion_events", "project_goal_loops", "project_goal_cycles", "project_goal_events",
        "project_progress_snapshots", "project_progress_deltas", "task_progress_snapshots",
        "task_progress_deltas", "context_runs", "skill_runs", "remote_step_up_sessions",
    ),
    # R0 rebuildable projections / caches
    _K.CACHE_ENTRY: ("context_packets", "context_packet_items", "p6_21h_product_read_index",
                     "state_proof_projections"),
}


def _build() -> dict[str, tuple[StorageCategory, DataKind]]:
    table_map: dict[str, tuple[StorageCategory, DataKind]] = {}
    for kind, tables in _BY_KIND.items():
        for table in tables:
            if table in table_map:
                raise ValueError(f"TABLE_MAPPED_TWICE:{table}")
            table_map[table] = (policy_for(kind).category, kind)
    return table_map


TABLE_CATALOG: dict[str, tuple[StorageCategory, DataKind]] = _build()
