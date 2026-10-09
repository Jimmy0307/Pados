# Retention Matrix (Data Lifecycle V1)

Generated from `pados.lifecycle.retention.CATALOG`; `tests/lifecycle/test_lifecycle_docs.py` fails if this
table and the catalog disagree. Windows come from `RetentionPolicy` (R0 default 24h, max 7d; R1 default 14d,
7-30d unless `explicit_override=True`). R2 and R3 have no age window.

| DataKind | Storage category | Retention class | Purge rule | Payload retention | Authoritative |
| --- | --- | --- | --- | --- | --- |
| `CACHE_ENTRY` | `CACHE` | `R0_EPHEMERAL` | `DELETE_RECORD` | - | no |
| `CAPABILITY_INTERMEDIATE_ARTIFACT` | `ARTIFACTS` | `R0_EPHEMERAL` | `DELETE_RECORD` | - | no |
| `TEMPORARY_FILE` | `CACHE` | `R0_EPHEMERAL` | `DELETE_RECORD` | - | no |
| `DEBUG_TRACE` | `OPERATIONAL_LOGS` | `R0_EPHEMERAL` | `DELETE_RECORD` | - | no |
| `PROBE_DEBUG` | `OPERATIONAL_LOGS` | `R0_EPHEMERAL` | `DELETE_RECORD` | - | no |
| `TEMPORARY_UPLOAD` | `REMOTE_UPLOADS` | `R0_EPHEMERAL` | `DELETE_RECORD` | - | no |
| `RAW_INTERMEDIATE_OUTPUT` | `SANDBOX_DATA` | `R0_EPHEMERAL` | `DELETE_RECORD` | - | no |
| `SANDBOX_WORKING_DATA` | `SANDBOX_DATA` | `R0_EPHEMERAL` | `DELETE_RECORD` | - | no |
| `FAILED_INTERMEDIATE_ARTIFACT` | `ARTIFACTS` | `R1_OPERATIONAL` | `DELETE_RECORD` | - | no |
| `HERDR_TELEMETRY` | `HERDR_TELEMETRY` | `R1_OPERATIONAL` | `DELETE_RECORD` | - | no |
| `CONNECTIVITY_HISTORY` | `OPERATIONAL_LOGS` | `R1_OPERATIONAL` | `DELETE_RECORD` | - | no |
| `MACHINE_CONNECTION_HISTORY` | `OPERATIONAL_LOGS` | `R1_OPERATIONAL` | `DELETE_RECORD` | - | no |
| `CAPABILITY_STAGE_EVIDENCE` | `OPERATIONAL_LOGS` | `R1_OPERATIONAL` | `DELETE_RECORD` | - | no |
| `NOTIFICATION_DELIVERY` | `OPERATIONAL_LOGS` | `R1_OPERATIONAL` | `DELETE_RECORD` | - | no |
| `OPERATIONAL_LOG` | `OPERATIONAL_LOGS` | `R1_OPERATIONAL` | `DELETE_RECORD` | - | no |
| `TELEMETRY` | `OPERATIONAL_LOGS` | `R1_OPERATIONAL` | `DELETE_RECORD` | - | no |
| `QUARANTINED_UPLOAD` | `REMOTE_UPLOADS` | `R1_OPERATIONAL` | `DELETE_RECORD` | - | no |
| `ACTIVE_ARTIFACT` | `ARTIFACTS` | `R2_PROJECT` | `HUMAN_DECISION_ONLY` | - | no |
| `PROJECT_EVIDENCE` | `ARTIFACTS` | `R2_PROJECT` | `HUMAN_DECISION_ONLY` | - | no |
| `DERIVED_DOCUMENT` | `ARTIFACTS` | `R2_PROJECT` | `HUMAN_DECISION_ONLY` | - | no |
| `GOVERNED_PROJECT_CONTENT` | `PROJECT_SOURCES` | `R2_PROJECT` | `HUMAN_DECISION_ONLY` | - | no |
| `PROJECT_SOURCE` | `PROJECT_SOURCES` | `R2_PROJECT` | `HUMAN_DECISION_ONLY` | - | no |
| `RELEASE_TEST_EVIDENCE` | `RELEASE_TEST_EVIDENCE` | `R2_PROJECT` | `HUMAN_DECISION_ONLY` | - | no |
| `REJECTED_ARTIFACT_PAYLOAD` | `ARTIFACTS` | `R3_AUTHORITY` | `PURGE_PAYLOAD_KEEP_METADATA` | `R1_OPERATIONAL` | yes |
| `ACCEPTANCE_STATUS` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `ARTIFACT_HASH` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `AUDIT_CHAIN` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `CAS_TRANSITION` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `DECISION_IDENTITY` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `EXECUTION_RECEIPT_SUMMARY` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `PAYLOAD_TOMBSTONE` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `REMOTE_INTENT_LEDGER` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `DEPLOYMENT_APPROVAL_LEDGER` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `PRIVILEGED_GRANT_LEDGER` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `MACHINE_INVENTORY` | `AUTHORITY_METADATA` | `R2_PROJECT` | `HUMAN_DECISION_ONLY` | - | no |
| `STORAGE_PLACEMENT_INDEX` | `AUTHORITY_METADATA` | `R2_PROJECT` | `HUMAN_DECISION_ONLY` | - | no |
| `OUTCOME_RECORD` | `OPERATIONAL_LOGS` | `R1_OPERATIONAL` | `DELETE_RECORD` | - | no |
| `EVALUATION_RECORD` | `ARTIFACTS` | `R2_PROJECT` | `HUMAN_DECISION_ONLY` | - | no |
| `ADVICE_LOG` | `OPERATIONAL_LOGS` | `R1_OPERATIONAL` | `DELETE_RECORD` | - | no |
| `TRAINING_JOB_EVENT_LOG` | `ARTIFACTS` | `R2_PROJECT` | `HUMAN_DECISION_ONLY` | - | no |
| `MODEL_REGISTRY_LEDGER` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `TRAINING_DECISION_LEDGER` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `CAPABILITY_DECISION_LEDGER` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `RESULT_RECEIPT_SUMMARY` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `SCOPE_VERSION_TRANSITION` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |
| `SOURCE_IDENTITY` | `AUTHORITY_METADATA` | `R3_AUTHORITY` | `NEVER` | - | yes |

## Purge rule meaning

| Rule | Effect in `pados.lifecycle.purge` |
| --- | --- |
| `DELETE_RECORD` | Whole item may be deleted once its R0/R1 window elapsed, only through an approved plan. |
| `PURGE_PAYLOAD_KEEP_METADATA` | Bytes may be purged after `payload_retention` elapsed since the rejection decision, with rejection proof; a hash-chained tombstone keeps the metadata. |
| `HUMAN_DECISION_ONLY` | Never in a purge plan. Removal is a human project decision outside Lifecycle V1. |
| `NEVER` | Authority metadata. Never in a purge plan, never deleted. |

## Database tables by category

Size accounting only (`account_database`); no DB rows are purged in V1. Source: `pados.lifecycle.tables.TABLE_CATALOG`.

| Storage category | DataKind | Tables |
| --- | --- | --- |
| `ARTIFACTS` | `ACTIVE_ARTIFACT` | `artifacts`, `worker_job_artifacts` |
| `ARTIFACTS` | `PROJECT_EVIDENCE` | `capability_delta_candidates`, `checkpoints`, `development_artifacts`, `development_backfill_records`, `development_change_items`, `development_change_sets`, `memory_artifacts` |
| `AUTHORITY_METADATA` | `ACCEPTANCE_STATUS` | `capability_registry`, `capability_versions`, `development_validation_receipts`, `result_acceptance_candidates` |
| `AUTHORITY_METADATA` | `ARTIFACT_HASH` | `state_proof_objects` |
| `AUTHORITY_METADATA` | `AUDIT_CHAIN` | `audit_events`, `memory_authority_events`, `state_proof_semantic_audit`, `state_proof_semantic_audit_heads` |
| `AUTHORITY_METADATA` | `CAS_TRANSITION` | `state_proof_commits` |
| `AUTHORITY_METADATA` | `DECISION_IDENTITY` | `approval_step_up_challenges`, `architecture_decisions`, `capability_changes`, `control_approvals`, `development_decisions`, `memory_decisions`, `project_human_gates`, `residual_plan_amendments`, `result_acceptance_decisions` |
| `AUTHORITY_METADATA` | `RESULT_RECEIPT_SUMMARY` | `state_proof_results` |
| `AUTHORITY_METADATA` | `SCOPE_VERSION_TRANSITION` | `state_proof_scope_heads`, `state_proof_scope_versions` |
| `CACHE` | `CACHE_ENTRY` | `context_packet_items`, `context_packets`, `p6_21h_product_read_index`, `state_proof_projections` |
| `OPERATIONAL_LOGS` | `OPERATIONAL_LOG` | `agent_handoffs`, `agent_runs`, `context_runs`, `control_messages`, `control_sync_events`, `control_work_requests`, `corpus_ingestion_batches`, `corpus_ingestion_events`, `development_events`, `development_sessions`, `project_controller_leases`, `project_goal_cycles`, `project_goal_events`, `project_goal_loops`, `project_progress_deltas`, `project_progress_snapshots`, `remote_step_up_sessions`, `run_events`, `skill_runs`, `task_loop_events`, `task_loop_iterations`, `task_loop_leases`, `task_loops`, `task_progress_deltas`, `task_progress_snapshots`, `worker_job_events`, `worker_jobs` |
| `PROJECT_SOURCES` | `GOVERNED_PROJECT_CONTENT` | `cognitive_work_items`, `context_project_registry`, `goal_runtime_bindings`, `graph_edges`, `graph_groups`, `graph_intents`, `graph_nodes`, `graph_workspaces`, `memory_records`, `memory_relationships`, `project_gaps`, `project_goals`, `project_plans`, `project_replan_proposals`, `project_task_dependencies`, `project_tasks`, `projects`, `skills`, `task_dependencies`, `task_goals`, `task_plan_steps`, `task_plans`, `tasks` |
| `PROJECT_SOURCES` | `PROJECT_SOURCE` | `context_sources`, `corpus_chunks`, `corpus_document_relationships`, `corpus_document_versions`, `corpus_documents`, `corpus_evidence_spans`, `corpus_pages`, `corpus_sources`, `file_chunks`, `file_registry`, `wiki_pages`, `wiki_sources` |
| `RELEASE_TEST_EVIDENCE` | `RELEASE_TEST_EVIDENCE` | `review_findings`, `test_runs` |
