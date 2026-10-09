# PADOS Security Controls Matrix (Security Hardening V1)

This matrix covers the per-surface threat model in [THREAT_MODEL_V1.md](THREAT_MODEL_V1.md).
Evidence is `file:line` on branch `agent/security-hardening-v1`, base `973957e`. Status values:

- **VERIFIED**: an existing control, proven by an adversarial test.
- **FIXED**: the test failed before the hardening fix and passes after it. The pre-fix run is
  `runtime/artifacts/reports/security-hardening-v1/logs/hardening_tests_before_fixes.log`.
- **REQUIRED_FOR_\<agent\>**: a control the named workstream must deliver. The acceptance test is the
  check the integrator runs after merging.
- **ACCEPTED_RISK**: residual risk accepted for this release, with the rationale.

Test file abbreviations (all under `tests/security/`): **R** `test_hardening_remote.py`, **S**
`test_hardening_staging.py`, **X** `test_hardening_sandbox.py`, **N** `test_hardening_compute_node.py`,
**E** `test_hardening_enterprise.py`, **A** `test_hardening_authority.py`, **C** `test_hardening_ci_static.py`,
**CR05** `test_hardening_cr05.py` (review CR-05 regressions; per-finding table in THREAT_MODEL_V1.md).

## Remote Operations / Home Base

| ID | Threat | Control | Evidence | Test | Status |
|---|---|---|---|---|---|
| SH-R01 | Remote endpoint auth bypass | Every `/remote/v1` route authenticates actor+proof through `session_for`; missing, unknown, wrong, empty and pass-the-hash attempts all get 401 | `remote/routes.py:59-65`, `remote/auth.py:90-98` | R::test_every_remote_route_requires_an_authenticated_actor | VERIFIED |
| SH-R02 | Timing oracle on unknown actor; stored-hash replay | Dummy digest plus `hmac.compare_digest` on every attempt; the presented proof is hashed before the compare | `remote/auth.py:94-96` | R::test_unknown_actor_takes_the_same_constant_time_compare | VERIFIED |
| SH-R03 | Session theft or expiry | No bearer session exists: every request re-authenticates and gets a fresh `session_id`, so there is nothing to steal or expire | `remote/auth.py:98` | R::test_unknown_actor_takes_the_same_constant_time_compare | VERIFIED |
| SH-R04 | Deployment with default credentials or exposure | Guard refuses the shipped control-token hash and DB credential, a non-loopback DB, and wildcard/public/hostname/mapped/6to4/documentation binds. A failed guard returns 503 on every route | `remote/deployment.py:33-92`, `remote/routes.py:61` | R::test_deployment_guard_refuses_non_overlay_binds, R::test_deployment_guard_refuses_default_credentials_and_exposed_database, R::test_failed_deployment_guard_closes_every_route_even_for_valid_actors | VERIFIED |
| SH-R05 | ACL bypass across projects | Project allowlist on intents, uploads, run detail, attention, approvals, attachments and job control | `remote/auth.py:104`, `remote/service.py:140-153`, `remote/service.py:211-216`, `remote/service.py:222`, `remote/routes.py:167` | R::test_actor_cannot_read_decide_or_attach_across_projects | VERIFIED |
| SH-R06 | Replay after reconnect | Ledger replay returns the original outcome and entry hash; one work request | `remote/service.py:160-162`, `remote/service.py:199-209` | R::test_reconnect_replay_returns_the_original_outcome_without_redispatch | VERIFIED |
| SH-R07 | intent_id reuse (other body or other actor) | `INTENT_ID_REUSED` on request-fingerprint or actor mismatch; no outcome disclosed | `remote/service.py:205-206`, `remote/intent.py:145` | R::test_intent_id_reuse_by_another_body_or_actor_fails_closed | VERIFIED |
| SH-R08 | Duplicated execution by concurrent retry | In-flight reservation; the second request gets `INTENT_IN_FLIGHT` and a later retry replays | `remote/service.py:163-173` | R::test_concurrent_duplicate_intent_is_dispatched_once | FIXED (F-26) |
| SH-R09 | Approval double commit or rebinding | Binding to actor, candidate, manifest, run and ScopeVersion; CAS `expected_version`; replay never re-commits; a decided ref is no longer presentable | `remote/approval.py:81-98`, `remote/approval.py:113-156`, `remote/live.py:156-174` | R::test_approval_replay_and_reuse_never_double_commit, R::test_approval_is_bound_to_the_presented_run | VERIFIED |
| SH-R10 | Viewer escalates to approve or control | `may_approve`, `may_control_jobs` | `remote/auth.py:109-111`, `remote/approval.py:124-125`, `remote/service.py:189-190` | R::test_viewer_cannot_approve_reject_or_control | VERIFIED |
| SH-R11 | Client ships context, history or identity (CHAT HISTORY IS NOT PADOS MEMORY) | Forbidden keys, identity keys and unknown fields are rejected; context is rebuilt server-side | `remote/intent.py:38-54`, `remote/intent.py:181-190`, `remote/context.py:69-81` | R::test_client_cannot_ship_context_history_or_identity, R::test_attachments_must_be_staged_references | VERIFIED |
| SH-R12 | Unauthorized shell / SQL / model passthrough | No such route; segment denylist over every registered route | `remote/routes.py:1-7`, `remote/routes.py:186` | R::test_no_shell_terminal_sql_or_model_passthrough_route_exists | VERIFIED |
| SH-R13 | Memory DoS by streamed upload | Streaming body cap | `remote/routes.py:32-42`, `remote/routes.py:154` | R::test_chunked_upload_cannot_bypass_the_body_cap | FIXED (F-01) |
| SH-R14 | Plaintext credential in the actor registry | `REMOTE_PLAINTEXT_CREDENTIAL_FORBIDDEN` for prohibited/qualified credential keys at top level and per actor | `remote/auth.py:27-39`, `remote/auth.py:71-74` | R::test_actor_registry_refuses_plaintext_credential_fields, A::test_existing_persistence_paths_reject_plaintext_password_fields | FIXED (F-27) |
| SH-R15 | Ledger over-collection (goal text on every status read) | READ outcomes are ledgered as run ids plus a digest; all ledger records pass `assert_minimized` (`context` allow-listed: fingerprint summary) | `remote/service.py:95-108`, `remote/service.py:135-138` | R::test_status_reads_do_not_copy_projection_rows_into_the_intent_ledger, R::test_ledgered_context_is_the_fingerprint_summary_only | FIXED (F-19b), VERIFIED (F-19a) |
| SH-R16 | Model bypass of authority (MODEL OUTPUT != TRUTH) | A failed candidate is never committed or presented for approval; commit happens only after validation, then CAS | `product_loop/service.py:436`, `product_loop/service.py:468`, `remote/approval.py:159-181`, `state_proof/service.py:205-215` | R::test_model_output_cannot_reach_semantic_state_without_validation | VERIFIED |
| SH-R17 | Guard checks config, not the listening socket | The guarded launcher binds exactly `bind_host`, verifies `getsockname()` and attests it. A non-loopback request needs the attestation and the attested address and port; a plain uvicorn launch is loopback-only | `remote/serve.py:31-100`, `remote/deployment.py:114-174`, `remote/routes.py:57-65` | CR05::test_cr05_plain_uvicorn_launch_never_serves_the_overlay, CR05::test_cr05_launcher_refuses_wildcard_public_or_non_literal_binds, CR05::test_cr05_attested_launcher_serves_only_its_verified_socket, CR05::test_cr05_real_launcher_socket_end_to_end_on_loopback | FIXED (F-05, CR-05) |
| SH-R18 | Brute force of remote proofs | Failure budget per actor and source (429) with an alert | `remote/routes.py:59-65` (gap) | Acceptance: 20 wrong proofs and then the right one return 429 until the window expires | REQUIRED_FOR_CONNECTIVITY (F-12) |
| SH-R19 | Weak operator-chosen proof | Proofs generated with at least 128 bits of entropy (`secrets.token_hex(16)`); only the SHA-256 is stored | `remote/auth.py:95` | — | ACCEPTED_RISK (F-13): an unsalted fast hash is acceptable only for high-entropy generated proofs |
| SH-R20 | Multi-process ledger fork / duplicate dispatch | OS file lock on append; an atomic persisted claim before dispatch; at-most-once (an unresolved claim is never re-dispatched); no-follow opens; private 0600 lock | `remote/ledger.py:61-190`, `remote/service.py:183-199` | R::test_f15_two_ledger_instances_in_separate_processes_do_not_fork_the_chain, CR05::test_cr05_two_instances_sharing_a_ledger_dispatch_an_intent_once, CR05::test_cr05_claim_is_atomic_across_processes, CR05::test_cr05_claim_left_by_a_crash_is_unresolved_and_never_redispatched, CR05::test_cr05_lock_is_private_and_unreplaceable | FIXED (F-15, CR-05) |

## File staging / bulk transfer

| ID | Threat | Control | Evidence | Test | Status |
|---|---|---|---|---|---|
| SH-S01 | Path traversal / device / ADS names | Plain-name validator; storage under a fixed name | `remote/staging.py:55-67`, `remote/staging.py:176` | S::test_upload_names_are_plain_file_names_only, S::test_staged_content_never_lands_under_the_client_chosen_name | VERIFIED |
| SH-S02 | Executable or over-ceiling content used | Quarantine; attachments must be STAGED and in the same project | `remote/staging.py:143-152`, `remote/service.py:140-148` | S::test_executable_and_over_ceiling_uploads_are_quarantined_not_usable | VERIFIED |
| SH-S03 | Inbox symlink swap (TOCTOU) | `O_NOFOLLOW` open plus an inode identity check; read bounded by `max_bytes` | `remote/staging.py:229-244` | S::test_bulk_inbox_symlink_swapped_in_after_the_check_is_not_followed | FIXED (F-03) |
| SH-S04 | Storage amplification / retention | Per-actor quota, content-addressed blob with refcount, R1 purge of staging and presentation files | `remote/staging.py:154-176` (gap) | Acceptance: staging the same bytes twice stores one blob; purge removes expired records | REQUIRED_FOR_LIFECYCLE (F-11) |

## Local Compute Node

| ID | Threat | Control | Evidence | Test | Status |
|---|---|---|---|---|---|
| SH-N01 | Arbitrary probe target / SSRF | Explicit private allowlist; http(s) only; no userinfo | `compute_node/config.py:28-36`, `compute_node/config.py:104-128`, `compute_node/registry.py:60-66` | N::test_private_host_check_is_an_explicit_allowlist, N::test_documented_private_ranges_remain_accepted, N::test_node_registration_refuses_public_credentialed_or_non_http_endpoints | FIXED (F-23) |
| SH-N02 | Identity spoofing by hostname/IP | Pinned `node_id` plus HMAC proof over a PADOS nonce | `compute_node/registry.py:181-190`, `compute_node/identity.py:46-52` | N::test_impostor_at_the_registered_address_cannot_prove_identity, N::test_same_endpoint_with_another_node_id_is_not_that_node, N::test_proof_without_the_credential_is_never_accepted | VERIFIED |
| SH-N03 | Unauthenticated node API | Constant-time Bearer check on every node route | `compute_node/service.py:93-96`, `compute_node/service.py:173` | N::test_node_service_rejects_requests_without_the_credential | VERIFIED |
| SH-N04 | Capability / classification / project bypass | Registration gate before probing; the router selects only among eligible candidates; node-side admission | `compute_node/provider.py:254-263`, `compute_node/provider.py:154`, `compute_node/jobs.py:121-133` | N::test_unauthorized_registration_never_routes, N::test_unregistered_node_is_unknown_to_pados, N::test_node_refuses_capabilities_and_classifications_it_does_not_advertise | VERIFIED |
| SH-N05 | Stale telemetry treated as live | Routing always re-probes; stale flag on old probes | `compute_node/provider.py:120`, `compute_node/visibility.py:30` | N::test_routing_reprobes_and_ignores_persisted_online_state, N::test_old_probe_is_flagged_stale | VERIFIED |
| SH-N06 | Future/naive probe time looks fresh or crashes | Unverifiable time means stale | `compute_node/visibility.py:52-62` | N::test_unverifiable_probe_time_is_never_treated_as_fresh | FIXED (F-25) |
| SH-N07 | Credential echo into evidence; probe DoS | Bounded, credential-free error codes; bounded log reason | `compute_node/registry.py:172-179`, `compute_node/registry.py:284-287` | N::test_node_error_detail_cannot_plant_credentials_or_break_probes, N::test_long_node_health_reason_does_not_crash_probe_or_routing | FIXED (F-24) |
| SH-N08 | Credential in probe files or receipts | Receipts carry `credential_ref` only; probe evidence has no secret | `compute_node/receipts.py:36`, `compute_node/registry.py:224-233` | N::test_probe_evidence_and_receipts_carry_refs_not_credentials | VERIFIED |
| SH-N09 | Plaintext node credential in config | SecretRef required; `extra="forbid"` | `compute_node/config.py:38-42`, `compute_node/config.py:86-91` | N::test_node_config_refuses_plaintext_credentials | VERIFIED |
| SH-N10 | Sniffed Bearer defeats HMAC identity; descriptor not MAC-bound | Separate auth and proof keys (HKDF), MAC over the response body, https for non-loopback | `compute_node/registry.py:150`, `compute_node/identity.py:50` (gap) | Acceptance: a recorded request/response cannot produce a valid proof for a new nonce; a modified descriptor fails `NODE_DESCRIPTOR_MAC_INVALID` | REQUIRED_FOR_COMPUTE_NODE (F-04) |
| SH-N11 | Stale node shown with its last `state` | Projection and clients render `stale: true` as UNKNOWN, never ONLINE | `compute_node/visibility.py:34` | Acceptance: Mission Control renders a stale ONLINE probe as UNKNOWN | REQUIRED_FOR_CONNECTIVITY |
| SH-N12 | Link-local metadata endpoint registered | Operator-authored registry; fixed path `/capabilities` | `compute_node/config.py:104` | — | ACCEPTED_RISK (F-17) |

## Sandbox Fabric

| ID | Threat | Control | Evidence | Test | Status |
|---|---|---|---|---|---|
| SH-X01 | Artifact names escape the outbox / host write | Plain-name validation on outputs | `sandbox/adapter.py:33-42`, `sandbox/adapter.py:122-124` | X::test_sandbox_output_names_cannot_escape_the_outbox, X::test_sandbox_output_absolute_names_cannot_write_the_host | FIXED (F-02) |
| SH-X02 | Drive-relative / ADS / device input names | Plain-name validation on inputs | `sandbox/adapter.py:115-116` | X::test_sandbox_input_names_reject_drive_stream_device_and_empty_forms, X::test_benign_artifact_exchange_still_verifies | FIXED (F-02) |
| SH-X03 | Sandbox reaches host/LAN/Internet/management | Hard deny, independent of rules | `sandbox/policy.py:65-84` | X::test_explicit_allow_rules_cannot_open_host_or_lan, X::test_no_profile_reaches_internet_or_management | VERIFIED |
| SH-X04 | Lab profile misuse | L2 opt-in, VM isolation, synthetic targets | `sandbox/policy.py:117-130` | X::test_lab_profiles_require_vm_isolation_explicit_opt_in_and_synthetic_targets | VERIFIED |
| SH-X05 | Host mounts / symlink escape | Realpath-based staging-only mounts; protected overlap refused | `sandbox/exchange.py:34-51` | X::test_host_paths_are_never_mountable, X::test_symlink_from_staging_to_protected_repository_is_refused | VERIFIED |
| SH-X06 | Executable or oversized exfiltration | Egress type/size/classification policy | `sandbox/policy.py:146-159` | X::test_egress_policy_admits_only_bounded_text_artifacts_and_classified_inputs | VERIFIED |
| SH-X07 | Option injection into registered commands | A leading-dash target is passed as `./<path>` | `worker/command.py:84-86` | A::test_f10_target_starting_with_dash_cannot_become_a_tool_option | FIXED (F-10) |

## Enterprise retrieval, secrets, observability

| ID | Threat | Control | Evidence | Test | Status |
|---|---|---|---|---|---|
| SH-E01 | Retrieval ACL bypass | Two-stage scope + presentation filter; RESTRICTED needs an explicit principal or role | `enterprise/authorization.py:124-130`, `enterprise/authorization.py:155-179` | E::test_source_acl_role_and_restricted_default_deny | VERIFIED |
| SH-E02 | Request rebinding to another project/tenant/principal | Request must equal the access context; capability bound to the project | `enterprise/pipeline.py:162-165`, `enterprise/authorization.py:78-79` | E::test_principal_cannot_rebind_request_to_another_project_tenant_or_identity | VERIFIED |
| SH-E03 | Revoked lineage still retrievable | Revocation mask on source and descendants; re-ingested identical content stays revoked | `enterprise/pipeline.py:166-167`, `enterprise/lineage.py:193-203` | E::test_revoked_source_is_excluded_and_stays_excluded_after_reingest | VERIFIED |
| SH-E04 | Revoked text leaks through generated output | Leak-guard texts include revoked sources | `enterprise/pipeline.py:228-235` | E::test_revoked_text_is_part_of_the_output_leak_guard, E::test_restricted_text_cannot_be_copied_into_generated_output | FIXED (F-28) |
| SH-E05 | SecretValue rendering / redaction | Non-rendering value; redact resolved and credential-shaped material | `enterprise/secrets.py:35-49`, `enterprise/secrets.py:93-99` | E::test_secret_value_never_renders_and_redaction_removes_resolved_material | VERIFIED |
| SH-E06 | Unauthorized SecretRef resolution | Authorized-ref subset; strict ref syntax; cleared after scope | `enterprise/secrets.py:75-90`, `execution/authorization.py:84-89` | E::test_secret_boundary_resolves_only_authorized_references | VERIFIED |
| SH-E07 | Content or credentials in logs | Attribute allowlist, length and scalar checks | `enterprise/observability.py:41-58` | E::test_observability_cannot_carry_content_or_credentials | VERIFIED |
| SH-E08 | HTTP reranker sends evidence anywhere | Private/userinfo validation where an `HttpReranker` is bound from config | `enterprise/rerank.py:105-120` (gap) | Acceptance: binding a public or credentialed rerank endpoint raises | REQUIRED_FOR_ENTERPRISE owner (F-29) |

## Authority, capability, memory, persistence

| ID | Threat | Control | Evidence | Test | Status |
|---|---|---|---|---|---|
| SH-A01 | Provider claims treated as receipts | Sealed, binding-checked receipts; SIMULATION / not-ok / `semantic_authority` never verify | `execution/receipts.py:62-126` | A::test_provider_claims_do_not_verify_as_receipts | VERIFIED |
| SH-A02 | Artifact path escape | Relative, no `..`, no drive or backslash | `execution/receipts.py:26-31` | A::test_artifact_handoff_paths_cannot_escape | VERIFIED |
| SH-A03 | Capability bypass (installed/registered != authorized; role labels) | Project-bound grant; role ignored; MCP must be registered and granted | `execution/authorization.py:16-69` | A::test_unregistered_or_ungranted_capabilities_never_execute, A::test_role_label_and_installation_are_not_grants | VERIFIED |
| SH-A04 | Terminal/session data entering Memory | Memory candidate and event writes reject C0/C1/ESC sequences | `memory/security.py:20`, `memory/security.py:36-43`, `memory/service.py:216`, `memory/service.py:697` | A::test_raw_terminal_output_cannot_enter_memory, A::test_ordinary_multiline_memory_text_is_still_accepted | FIXED (F-07) |
| SH-A05 | Provider tokens entering Memory | Execution-context credential detector added to memory writes | `memory/security.py:36-41` | A::test_secret_material_cannot_enter_memory | FIXED (F-07) |
| SH-A06 | Plaintext password persistence (new points) | Shared `assert_minimized` | `lifecycle/minimization.py:70-104` | A::test_shared_minimization_guard_blocks_plaintext_passwords_and_session_data | VERIFIED |
| SH-A07 | Weaker detector on decisions and markdown sources | `record_decision` / `register_markdown_source` call `assert_memory_content` | `memory/service.py:151`, `memory/service.py:727` (gap) | Acceptance: `record_decision(question="ghp_…")` and ESC text raise | REQUIRED_FOR_LIFECYCLE (F-18) |
| SH-A08 | Run context persisted unminimized | `_run_context_payload` passes `assert_minimized` (with reviewed allow fields) | `product_loop/service.py:626-653` (gap) | Acceptance: a GoalRequest whose context carries a prohibited key is refused before persistence | REQUIRED_FOR_LIFECYCLE / product-loop owner (F-20) |

## CI/CD, release, backup, connectivity

| ID | Threat | Control | Evidence | Test | Status |
|---|---|---|---|---|---|
| SH-C01 | Write-capable CI token | Workflow-level `permissions: contents: read`; no `write-all` | `.github/workflows/ci.yml:4-5` | C::test_workflows_exist_and_have_read_only_default_token | VERIFIED |
| SH-C02 | Public PR reaching personal infrastructure or secrets | No `pull_request_target`; PR jobs never `self-hosted`, never `secrets.*`, never ssh/scp/rsync, never non-loopback IPs | `.github/workflows/ci.yml:3-45` | C::test_public_pull_requests_cannot_reach_personal_infrastructure_or_secrets | VERIFIED (REQUIRED_FOR_CICD to keep it green) |
| SH-C03 | CI writing semantic authority | No `pados` authority subcommands or authority calls in workflow steps | `.github/workflows/ci.yml:20-29` | C::test_ci_cannot_invoke_pados_semantic_authority | VERIFIED (REQUIRED_FOR_CICD to keep it green) |
| SH-C04 | Release tooling importing authority modules | `scripts/release`, `src/pados/release` import no state_proof/control/memory/remote-live modules | `scripts/release/*.py`, `src/pados/release/package.py` | C::test_release_and_ci_scripts_do_not_import_authority_modules | VERIFIED (REQUIRED_FOR_CICD to keep it green) |
| SH-C05 | Mutable action tags | Actions pinned by 40-hex SHA | `.github/workflows/ci.yml:17-27` (gap) | Acceptance: every `uses:` matches `@[0-9a-f]{40}` | REQUIRED_FOR_CICD (F-08) |
| SH-C06 | Dependency tampering | Hash-locked install (`--require-hashes`) | `.github/workflows/ci.yml:20` (gap) | Acceptance: every lock line has `--hash=sha256:`; CI uses `--require-hashes` | REQUIRED_FOR_CICD (F-09) |
| SH-C07 | Credential-shaped content in release artifacts | Release secret scan passes without widening its allowlist | `release/package.py:75-86` | `tests/security/test_p6_6_adversarial.py -k release_zip` (fails at base on lifecycle files) | REQUIRED_FOR_LIFECYCLE (F-06) |
| SH-B01 | Backup confidentiality and integrity | Encrypted dump plus SHA-256; both DB lanes and the object store; no default credential fallback | `scripts/backup.ps1:20-46` (gap) | Acceptance: restore smoke verifies hashes and CAS object refs from the backup set | REQUIRED_FOR_LIFECYCLE (F-21) |
| SH-K01 | Notification outbox persistence | Persisted outbox calls `assert_minimized` and keeps push payload keys within `PUSH_ALLOWED_KEYS` | `remote/notifications.py:25`, `remote/notifications.py:78-115` | Acceptance: persisted outbox file contains no goal, project name or detail text | REQUIRED_FOR_CONNECTIVITY |
| SH-K02 | Connectivity snapshot freshness | Future/naive/unparseable `observed_at` means stale; no crash | `connectivity/config.py:91` (gap) | Acceptance: the cases of N::test_unverifiable_probe_time_is_never_treated_as_fresh replayed against `connectivity_visibility` | REQUIRED_FOR_CONNECTIVITY (F-22) |
| SH-K03 | Connectivity probe targets | `overlay_probe.host` must be a literal PRIVATE_OVERLAY address; `wan_probe` is fixed per mode (no arbitrary host) | `connectivity/config.py:33-41`, `connectivity/config.py:56-60` | Acceptance: loading a config with a hostname or public overlay probe raises | REQUIRED_FOR_CONNECTIVITY |
| SH-H01 | Herdr telemetry promoted to memory or state | Herdr writers call `assert_minimized`; there is no path to `add_memory_record` or receipts | `src/pados/herdr/boundary.py`, `adapter.py`, `store.py` | `tests/herdr/test_boundary.py` (static import closure + subprocess import check), `tests/herdr/test_cr06_findings.py` | VERIFIED (Herdr V1; Codex CR-06/CR-06b) |
| SH-M01 | Machine Connection credential handling | SecretRef only; bootstrap password never persisted; host-key pinning; privileged ops gated by grant and approval | `src/pados/machines/profile.py`, `identity.py`, `bootstrap.py`, `privilege.py`, `ssh.py` | `tests/machines/test_identity_profile.py`, `test_bootstrap.py`, `test_connection_states.py`, `test_privilege.py`, `test_cr07b.py` | VERIFIED (Machine Connection V1; Codex CR-07/CR-07b/CR-07c) |

## Fixer round 1 (review CR-05) additions

| ID | Threat | Control | Evidence | Test | Status |
|---|---|---|---|---|---|
| SH-S05 | FIFO swapped in blocks the inbox open; content mutated during the read | Non-blocking no-follow open, fstat and inode check, bounded copy to a PADOS-owned snapshot, size/mtime re-check | `remote/staging.py:237-271` | CR05::test_cr05_fifo_swapped_in_after_the_check_cannot_block_the_open, CR05::test_cr05_inbox_file_modified_while_copied_is_refused | FIXED |
| SH-X08 | Windows-normalized device names, trailing dots/spaces, case collisions | `_plain_name`, `_claim_name` | `sandbox/adapter.py:38-56` | CR05::test_cr05_windows_normalized_device_and_trailing_names_are_refused, CR05::test_cr05_case_insensitive_collisions_across_inputs_and_outputs_are_refused | FIXED |
| SH-R21 | A single oversized chunk copied before the cap check | Check before extending | `remote/routes.py:33-45` | CR05::test_cr05_single_oversized_chunk_is_refused_before_it_is_copied | FIXED |
| SH-N13 | Node descriptor reasons persisted or logged verbatim | Local code mapping; only the code is logged | `compute_node/registry.py:150-175` | CR05::test_cr05_descriptor_reasons_are_mapped_to_local_codes | FIXED |
| SH-N14 | Malformed node JSON escapes the controlled error path | Object/string schema checks give local codes | `compute_node/registry.py:136-145`, `compute_node/registry.py:200-208` | CR05::test_cr05_malformed_node_responses_become_local_error_codes | FIXED |
| SH-N15 | Endpoint validated differently from how the client parses it | `private_http_url` (`httpx.URL`); the client receives the parsed form | `compute_node/config.py:132-148` | CR05::test_cr05_backend_endpoints_are_parsed_like_the_http_client, CR05::test_cr05_backend_endpoints_refuse_what_the_client_would_send_elsewhere | FIXED |
| SH-R22 | Nested plaintext credentials / unknown actor fields ignored | Recursive check; closed actor-row schema | `remote/auth.py:45-72` | CR05::test_cr05_nested_plaintext_credentials_are_refused, CR05::test_cr05_actor_rows_have_a_closed_schema | FIXED |


## Phase 7 — Herdr / Machine security review (independent Codex reviewer)

| Review | Scope | Result |
| --- | --- | --- |
| CR-06 → CR-06b | Herdr adapter/store/visibility | 8 findings (3 HIGH) fixed; M04 `setsid()` escape ACCEPTED_RISK; N01 fixed by integrator (fdbb75c) |
| CR-07 → CR-07b → CR-07c | Machine Connection | 8 + 3 findings (5 HIGH) fixed over 2 fixer cycles; residuals: ledger+head restore without external anchor, consistent rewrite with key, POSIX-only owner checks (ACCEPTED_RISK) |

Explicit Phase 7 questions → answers (evidence in `runtime/artifacts/reports/herdr-integration-v1/` and
`runtime/artifacts/reports/machine-connection-v1/`): Herdr compromise cannot become semantic authority (no import path;
telemetry-only meanings; closed vocabularies); terminal output cannot enter Memory or the store (no free-text field,
terminal keys dropped, keyed digests); machine passwords never reach config/log/receipt/exception (frame-walk tests);
CI cannot discover SSH/private credentials (inventory holds references only; credential use requires a separate
operator policy); telemetry growth is bounded (count/bytes/R1); stale state is never live (source snapshot time,
latest-snapshot membership, collection outcome, revision digests); mobile/remote access cannot satisfy privileged
grants (RemoteSession refused; HMAC operator proofs; ledger-sourced authorization); identity cannot be spoofed by
hostname/IP (host-key identity, HostKeyAlias, DNS re-check); no second scheduler (collection only on explicit call);
Herdr/session state is never semantic acceptance; public export/provenance checks remain green.
