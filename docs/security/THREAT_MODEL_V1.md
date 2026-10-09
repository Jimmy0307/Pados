# PADOS threat model V1 (0.1.0a1)

**Scope.** LOCAL_SINGLE_USER and TRUSTED_LAN profiles. PADOS does **not** claim perfect security. Residual risks are stated explicitly, and HARDENED_ENTERPRISE is out of scope for this release.

**Trust boundaries**

- **B1:** Human ↔ PADOS control plane (loopback API, control token).
- **B2:** PADOS ↔ providers, models and tools (Codex/Claude CLI, local models, MCP).
- **B3:** PADOS ↔ compute nodes (private network).
- **B4:** PADOS ↔ enterprise sources and retrieval.
- **B5:** Workspace filesystem (SafeWorkspaceFS).
- **B6:** Databases: the operational lane, and the proof lane for CAS and audit.

**Status column.** ACCEPTED means the residual risk is accepted for alpha; DEFERRED means the mitigation is planned.

| # | Threat | Asset | Attacker | Boundary | Mitigation (evidence) | Residual risk | Status |
|---|---|---|---|---|---|---|---|
| 1 | Malicious provider (model/agent claims success, writes extra files) | Project state, workspace | Provider or model | B2, B5 | Provider output is data. ExecutionReceipt is bound to the contract hash and artifacts. Only declared paths are exported (CodexCliMaker scope diagnostic). Validation plus CAS are required (`MODEL OUTPUT != TRUTH`) | A provider may produce plausible but wrong content that passes weak acceptance criteria | ACCEPTED (criteria quality is the user's responsibility) |
| 2 | Compromised compute node | Artifacts, evidence confidentiality | Node owner or malware | B3 | Registration, authorization and capability allowlist. Classification ceilings. Record MAC. Independent SHA-256 verification. Core re-verification of workspace bytes (slice steps 8, 15) | A compromised authorized node sees the evidence sent to it and can return wrong (but hashed) output | ACCEPTED (route sensitive data only to trusted nodes) |
| 3 | Prompt injection in repository or evidence text | Authority chain | Content author | B2, B4 | Context is marked `instructions_are_data_not_permission`. Grants come only from PADOS. Agent role labels are ignored. Repository text cannot authorize (tests) | Injected text can still influence model output quality | ACCEPTED |
| 4 | Artifact tampering (node store, transit, workspace) | Artifact integrity | Local attacker | B3, B5 | Content-addressed read-only node store with integrity check. PADOS hash verification. Core receipt re-verification (`ARTIFACT_HASH_MISMATCH`, `ARTIFACT_TAMPERED`, validation failure) | An attacker with DB and file write access on the PADOS host is out of scope | ACCEPTED |
| 5 | Source poisoning | Evidence quality | Source author | B4 | Content-hash source identity, lineage, classification, revocation (dry-run plus projection mask). Evidence is never promoted to truth | Poisoned but authorized sources can mislead models; no content trust scoring | DEFERRED (source trust scoring) |
| 6 | Stale ScopeVersion | Semantic consistency | Concurrent writer | B6 | Expected-ScopeVersion CAS on every commit (STALE_CANDIDATE tests) | None known | ACCEPTED |
| 7 | Replayed receipt | Semantic state | Local attacker | B6, B3 | Receipts are bound to run id, attempt, contract hash and artifacts. Node records are bound to request id, run id, input hash and manifest hash via MAC. Idempotency keys | Node records have no expiry or nonce beyond request binding | DEFERRED (receipt freshness window) |
| 8 | Unauthorized MCP/tool | Execution scope | Misconfigured tool | B2 | MCP_TOOL is denied by default. Installed ≠ authorized. Project-bound grants | Tool behaviour inside an authorized grant is trusted | ACCEPTED |
| 9 | Classification leakage | RESTRICTED data | Insider or model | B2, B3, B4 | Two-stage authorization. Ceilings enforced before rerank and routing. Restricted-exposure budget. `WITHHELD_EVIDENCE_LEAK` guard. RESTRICTED routes only to explicitly RESTRICTED local registrations | Paraphrased leakage is not detected by the shingle guard | ACCEPTED (documented limit) |
| 10 | SecretRef exposure | Credentials | Log reader | All | SecretRefs only. `SecretValue` never renders. Redaction. Observability allowlist. Secret scans (gates X02; release scanner) | The resolved secret exists in process memory during the call | ACCEPTED |
| 11 | Malicious retrieval result | Context integrity | Backend | B4 | Backends are scope-filtered before scoring. Presentation filter. Evidence has provenance and a package hash | A compromised in-process backend is trusted code | ACCEPTED |
| 12 | Node impersonation | Node identity | LAN attacker | B3 | Registration pins `node_id`. HMAC identity proof over a PADOS nonce. Credential via SecretRef | HMAC shares one secret per node; no revocation list; the LAN is assumed trusted | ACCEPTED for TRUSTED_LAN; DEFERRED to asymmetric identity for hardened profiles |
| 13 | Compromised local network (MITM, sniffing) | Evidence confidentiality | LAN attacker | B3 | Private networks only, and public binds refused. Integrity is protected by MAC and hashes | **No transport encryption in V1**: evidence and prompts cross the LAN in cleartext unless the operator adds TLS | DEFERRED (TLS/mTLS, hardened profile) |
| 14 | Path traversal | Workspace or host files | Provider or request | B5 | FileChange paths must be relative with no `..`. Declared-path allowlist. Approved-root checks | None known | ACCEPTED |
| 15 | Symlink / reparse-point attacks | Host files | Local attacker | B5 | SafeWorkspaceFS: POSIX `O_NOFOLLOW`/dir-fd; Windows handle-based checks rejecting reparse points, short names, ADS and hard links (W01) | Windows symlink-creation privilege was not available for testing; covered with junctions | ACCEPTED |
| 16 | Cross-project data leakage | Project isolation | Other project | B4, B6 | Project-scoped scopes, grants, registrations (`project_ids`), memory isolation tests, per-project Mission Control grouping | Single-user deployments share one OS account | ACCEPTED |
| 17 | Default control token hash | Control API | Anyone with the public source | B1 | Loopback-only API. `pados init` generates a unique token. Doctor warns `DEFAULT_CONTROL_TOKEN_HASH_IN_USE` | A deployment that skips `pados init` accepts the publicly known default token from local processes | DEFERRED (BETA_BLOCKER: remove the default) |

---

# Security Hardening V1 — per-surface threat model (T-P2-SH)

**Base.** Commit `973957e` plus the hardening fixes on `agent/security-hardening-v1`. Evidence is
`file:line` against that tree. Control IDs (`SH-*`) and their status (VERIFIED / FIXED /
REQUIRED_FOR_<agent> / ACCEPTED_RISK) are in [SECURITY_CONTROLS_MATRIX.md](SECURITY_CONTROLS_MATRIX.md).
Findings (`F-*`) are listed at the end of this document. Every VERIFIED/FIXED control has an
adversarial test in `tests/security/test_hardening_*.py`.

**Frozen principles that this model checks, never reopens.** MODEL OUTPUT != TRUTH. EXECUTION
COMPLETE != SEMANTIC ACCEPTANCE. REMOTE CLIENT SENDS INTENT, NOT CONTEXT. CHAT HISTORY IS NOT PADOS
MEMORY. PROVIDER IS NOT AUTHORITY. CI SUCCESS != DEPLOYMENT AUTHORIZATION. BUILD SUCCESS != TRUST.
Installed/registered != authorized. One router (`pados.worker.router.select_worker`), one CAS path,
one semantic commit path, one audit authority.

**Additional trust boundaries.**

- **B7:** Remote client ↔ Home Base over the private overlay (`/remote/v1`).
- **B8:** External bulk transport (SFTP) ↔ PADOS inbox directory.
- **B9:** PADOS ↔ sandbox (VM/container/simulated) through the artifact exchange.
- **B10:** Public GitHub (PRs, Actions runners) ↔ maintainer infrastructure and release artifacts.
- **B11:** PADOS ↔ backup media and restore targets.

## 1. Remote Operations (`src/pados/remote`)

- **Assets.** Approval authority (a remote APPROVE drives a CAS commit), run projections, staged
  uploads, the intent ledger, actor credentials (hashed).
- **Entry points.** Every route registered by `register_remote_routes` (`routes.py:45`); a
  standalone overlay process `create_remote_app` (`app.py:140`) or a mount on the main API.
- **Trust boundary.** B7. The client is untrusted: it supplies intent only.

| STRIDE | Threat | Existing control (evidence) | Gap / required control |
|---|---|---|---|
| S | Unauthenticated or forged actor | Per-request actor+proof, SHA-256 hash compared with `hmac.compare_digest`, dummy hash for unknown actors (`auth.py:90-98`); every route goes through `session_for` (`routes.py:59-65`) | No lockout or rate limit on 401 (F-12). Unsalted SHA-256 of the proof (F-13) |
| S | Pass-the-hash with the stored digest | The digest is re-hashed before compare (`auth.py:95`) | — (SH-R02) |
| T | Client ships context, history or identity | `FORBIDDEN_CONTEXT_KEYS`, `CLIENT_IDENTITY_FORBIDDEN`, unknown-field rejection (`intent.py:38`, `intent.py:187`) | — |
| T | Approval bound to a different candidate/run/version | `bind_remote_decision` + `apply_remote_decision` re-verify actor, candidate, manifest, ScopeVersion; CAS `expected_version` (`approval.py:87`, `approval.py:113-156`); presentation pinned by `proposal_hash` (`live.py:156-174`) | — |
| R | Remote actions not attributable | Hash-chained intent ledger with actor+session (`service.py:120-138`, `ledger.py:55`) | Chain has no anchored head, so tail truncation is undetectable (F-16). Multi-process appends can fork the chain (F-15) |
| I | Cross-project read (runs, uploads, approvals) | `authorize_project` (`auth.py:104`), `_owned_run` (`service.py:150`), upload ACL (`routes.py:167`), attention scoped to the actor's projects (`service.py:247`) | `/nodes` and `/mission-control` show every node to any actor (F-14) |
| I | Status reads copied goal text into the ledger | **FIXED**: `_ledger_view` keeps run ids and a digest (`service.py:95`); ledger records pass `assert_minimized` (`service.py:137`) | — |
| D | Chunked upload with no Content-Length was buffered without limit | **FIXED**: `bounded_body` streams with a hard cap (`routes.py:32`) | No per-actor storage quota; identical uploads are stored twice (F-11) |
| D | Duplicate execution: replay after reconnect, concurrent retry | Ledger replay guard keyed on `request_fingerprint` + actor (`service.py:199-209`); DB idempotency key `remote:<intent_id>` (`live.py:79`). **FIXED**: an atomic claim persisted in the ledger under the OS lock before dispatch (`ledger.py:159`, `service.py:183-199`). The claim is bound to actor and request fingerprint; an unresolved claim is never re-dispatched | Claim recovery after a crash is a human step (`IntentLedger.release`) |
| E | Viewer approves or controls jobs | `may_approve` (`auth.py:109`), `REMOTE_JOB_CONTROL_NOT_ALLOWED` (`service.py:190`) | — |
| E | Shell, SQL, filesystem or model passthrough | No such route exists; `remote_shell: NOT_PROVIDED` (`routes.py:186`) | — (SH-R12 enforces it on every new route) |
| E | Plaintext credential sitting in the actor registry | **FIXED**: `_assert_no_plaintext_credentials` (`auth.py:33`) | — |

## 2. Home Base and deployment

- **Assets.** The whole PADOS host. **Entry point.** The overlay bind.
- **Controls.** `check_remote_deployment` refuses the shipped control-token hash and DB credential,
  a non-loopback DB, wildcard/public/hostname binds, using an explicit overlay allowlist instead of
  `is_private` (`deployment.py:33`, `deployment.py:70-92`); a failed guard returns 503 on every route
  (`routes.py:61`).
- **F-05 (HIGH, FIXED in CR-05).** Original gap: the guard validates `RemoteConfig.bind_host`, but
  nothing made the ASGI server bind there. `create_remote_app` is a factory, and `uvicorn --host 0.0.0.0` passes the guard.
  Required (REQUIRED_FOR_CONNECTIVITY: owner of `remote/app.py` and the CLI): a single launcher that
  binds `config.bind_host`, plus a per-request check that `request.scope["server"]` is that address
  or loopback.

## 3. Local Compute Node (`src/pados/compute_node`)

- **Assets.** Prompts and evidence sent to nodes, artifacts returned, the node credential.
- **Entry points.** The node service (`service.py:167-231`); PADOS-side probes and execution
  (`registry.py:146-200`, `provider.py:110-252`).

| STRIDE | Threat | Existing control (evidence) | Gap / required control |
|---|---|---|---|
| S | Identity by hostname/IP | Registration pins `node_id`; `verified()` requires the `node_id` and an HMAC over a PADOS nonce (`registry.py:181-190`, `identity.py:50`); the node id derives from a random key (`identity.py:46`) | The bearer credential that keys the HMAC is sent in cleartext on every request (`registry.py:150`). One sniffed request lets an on-path attacker forge proofs, and the descriptor is not MAC-bound (F-04) |
| T | Tampered job record or artifact | `record_mac` over the full record, request binding, SHA-256 and size checks (`provider.py:199`, `provider.py:228-252`) | — |
| I | SSRF / arbitrary probe target | Registration and backend endpoints must be http(s), carry no userinfo and be private. **FIXED**: an explicit allowlist replaces `is_private`, which admitted 6to4/Teredo/NAT64 and reserved ranges (`config.py:104-128`) | Link-local `169.254.169.254` (cloud metadata) is still accepted, as documented (F-17, ACCEPTED_RISK) |
| I | Credential echoed into probe evidence, logs or Mission Control | **FIXED**: `_error_code` drops details that are over-long or contain the credential (`registry.py:172`); the observability reason is bounded (`registry.py:287`) | — |
| D | Malformed node text crashing probes or routing | **FIXED** as above (an over-long reason raised `OBSERVABILITY_ATTRIBUTE_TOO_LONG`) | — |
| E | Unregistered, unauthorized or out-of-scope node or capability executes | `_registration_gate` (`provider.py:254`); routing re-probes live (`provider.py:120`) and selects via `select_worker` (`provider.py:154`); the node rejects capabilities and classifications it does not serve (`jobs.py:122`) | — |
| T | Stale telemetry presented as live | `stale` flag from `probe_max_age_seconds` (`visibility.py:30`). **FIXED**: future, naive or unparseable timestamps count as stale (`visibility.py:52-62`) | The `state` field keeps the last value next to `stale: true`. Clients must render stale as unknown (REQUIRED_FOR_CONNECTIVITY, SH-N11) |

## 4. Sandbox Fabric (`src/pados/sandbox`)

- **Assets.** The host filesystem and LAN, and staged inputs. **Entry points.** `SandboxAdapter.run`,
  `assert_mount_allowed`.
- **Controls.** Hard deny of SANDBOX/LAB→HOST/LAN regardless of rules (`policy.py:72-74`); Internet
  and management are denied unless explicitly enabled, and no built-in profile enables them
  (`policy.py:68-71`); L2 requires an explicit opt-in, and lab profiles require VM isolation and
  synthetic targets (`policy.py:117-130`); mounts only strictly inside the staging root, after
  realpath, with protected-path overlap refused (`exchange.py:34-51`); artifact type and size
  egress policy (`policy.py:152-159`).
- **FIXED F-02 (HIGH).** Output artifact names from the sandboxed step were joined to the outbox
  unchecked: `../../x` and absolute paths wrote host files. Input names allowed `C:x` (drive-relative
  on Windows), ADS `a:b`, device names and empty or `.` names. `_plain_name` (`adapter.py:38`) now
  validates both directions.
- **Shell.** The worker runs only registered commands with `shell=False`. A `target` value beginning
  with `-` is passed as an option (`worker/command.py:84`), for example `ruff check --fix` (F-10).

## 5. File staging and bulk transfer (`remote/staging.py`)

- Upload names: plain names only; storage under a fixed name (`staging.py:55`, `staging.py:176`).
  Executable suffixes and over-ceiling classifications are quarantined (`staging.py:48`).
- **FIXED F-03 (MEDIUM).** `BulkInbox.register` checked `lstat`/`resolve` and then read by path. An
  inbox writer could swap in a symlink and stage any file the PADOS process can read.
  `_read_checked_file` (`staging.py:229-244`) opens with `O_NOFOLLOW` and requires the opened inode to
  be the checked one.

## 6. Machine Connection (future) — required design controls (REQUIRED_FOR_MACHINE_CONNECTION)

- Credentials only as `secretref://` (SSH key file reference, ssh-agent socket, OS credential store).
  A bootstrap password may be typed once, but it is never persisted, logged or ledgered. Every
  machine-profile writer calls `assert_minimized`; `password_supplied: bool` is the most that may be
  recorded.
- Identity is a pinned host key fingerprint (or an mTLS key), never a hostname or IP. A changed host
  key fails closed (`HOST_KEY_MISMATCH`) and requires a human re-pin.
- Privileged shell operations are not remote intents. Any shell capability is a registered command
  with an argument schema (the `worker/command.py` model, with the F-10 fix), gated by a project grant
  and human approval. No PTY or scrollback ever enters Memory: `memory/security.py:36` rejects
  terminal control sequences (FIXED F-07).

## 7. Herdr (future) — telemetry substrate only (REQUIRED_FOR_HERDR)

- Herdr output is R0/R1 operational telemetry. It never feeds `add_memory_record` or semantic state,
  and never becomes a ResultReceipt. Pane content and scrollback are prohibited fields
  (`lifecycle/minimization.py`). A telemetry record must carry `observed_at` and is shown stale under
  the same rules as SH-N05/SH-N06/SH-N11.

## 8. GitHub, CI, release artifacts, dependencies (B10)

- **Current workflow** (`.github/workflows/ci.yml`): `permissions: contents: read`, GitHub-hosted
  runners, no `secrets.*`, no `pull_request_target`. Verified statically by
  `test_hardening_ci_static.py`.
- **Required (REQUIRED_FOR_CICD).** Pin actions by commit SHA (all are tag-pinned: F-08). Install with
  `--require-hashes` from a hash-locked lock file (F-09). Never run PR code on self-hosted runners or
  with secrets. Release signing and publishing go in a separate, manually approved job on protected
  tags only. CI output is evidence, not authority: no CI job may import authority modules or call
  `commit_result` / `decide_approval` (static test SH-C03/SH-C04).
- **Release scanner.** `pados.release.package._scan_secret` fails at base `973957e` on
  `src/pados/lifecycle/retention.py` and `tests/lifecycle/test_retention_contract.py` (F-06). It must
  be fixed in those files, not by weakening the scanner.

## 9. Secrets, retention, backup, reconnect, notification retry

- **Secrets.** SecretRef only. `SecretValue` never renders (`enterprise/secrets.py:46`). Resolution
  happens only for authorized refs (`secrets.py:75-90`). Observability uses an attribute allowlist
  (`observability.py:48`). Memory used a weaker detector than execution context, so GitHub, Slack and
  AWS key shapes were accepted. **FIXED** for memory candidates and events
  (`memory/security.py:36`). `record_decision` and `register_markdown_source` still use the weaker
  `assert_no_secret` (F-18, REQUIRED_FOR_LIFECYCLE).
- **Retention.** Remote ledger, staging and decision-presentation files have no purge path in this
  tree (F-11, F-19, REQUIRED_FOR_LIFECYCLE). `product_loop/service.py:652` persists `bounded_context`
  without a minimization guard (F-20).
- **Backup** (`scripts/backup.ps1`, `BACKUP_RESTORE.md`). The dump is plaintext with no integrity
  hash. It covers one database only, not the proof/product lane or the content-addressed object
  store, and it falls back to the development DB credential (F-21).
- **Reconnect.** Intent replay returns the original outcome (SH-R06). Jobs are PADOS-owned, and a
  client disconnect never cancels a job (`jobs.py:120`).
- **Notification retry.** The push payload is metadata-only (`notifications.py:25`). The outbox is in
  memory (`notifications.py:78`). Any persisted outbox must call `assert_minimized` and keep
  `PUSH_ALLOWED_KEYS` (REQUIRED_FOR_CONNECTIVITY, SH-K01). Connectivity freshness has the same
  future/naive timestamp defects as compute-node visibility had: `connectivity/config.py:91` reports a
  future snapshot as fresh and crashes on a naive one (F-22, REQUIRED_FOR_CONNECTIVITY).

## Findings

| ID | Sev | Surface | Description | Evidence | Fix / recommendation |
|---|---|---|---|---|---|
| F-01 | MEDIUM | Remote upload | Chunked body (no Content-Length) buffered fully before any size check | `routes.py` pre-fix `await request.body()` | FIXED: `bounded_body` (`routes.py:32`); test `test_chunked_upload_cannot_bypass_the_body_cap` |
| F-02 | HIGH | Sandbox | Output names `../..`/absolute wrote outside the outbox, onto the host. Input names allowed drive/ADS/device/empty forms | pre-fix `adapter.py:101-112` | FIXED: `_plain_name` (`adapter.py:38`); `test_sandbox_output_*`, `test_sandbox_input_names_*` |
| F-03 | MEDIUM | Bulk inbox | TOCTOU: a symlink swapped in after `lstat`/`resolve` was followed by `read_bytes` | pre-fix `staging.py:213-219` | FIXED: `_read_checked_file` (`staging.py:229`); `test_bulk_inbox_symlink_swapped_in_after_the_check_is_not_followed` |
| F-04 | MEDIUM | Compute node | The node credential is sent as a cleartext Bearer value and is also the HMAC identity key, so one sniffed request defeats identity proof. The descriptor is not covered by the proof | `registry.py:150`, `identity.py:50` | REQUIRED_FOR_COMPUTE_NODE (protocol change): derive separate auth and proof keys (HKDF), sign the response body, require https on non-loopback under TRUSTED_LAN. Not changed here (protocol compatibility) |
| F-05 | HIGH | Home Base | Deployment guard checks the configured bind host, not the actual listening socket | `app.py:140`, `deployment.py:70` | FIXED (cc13fa0 + CR-05): guarded launcher `remote/serve.py:31-100` binds and attests the socket (`deployment.py:114`). Non-loopback requests require the attestation and the attested address and port (`deployment.py:145`, `routes.py:57`) |
| F-06 | MEDIUM | Release | Release secret scan fails at base on lifecycle files (enum value literals) | `release/package.py:75`; `tests/security/test_p6_6_adversarial.py -k release_zip` | REQUIRED_FOR_LIFECYCLE: restructure those literals; never widen `ALLOWED_LOCAL_DEFAULTS` |
| F-07 | MEDIUM | Memory | Raw terminal output (ANSI/OSC/C0/C1) and GitHub/Slack/AWS token shapes accepted by memory candidate/event writes | pre-fix `memory/security.py:13-32` | FIXED: `assert_memory_content` (`memory/security.py:36`); `test_raw_terminal_output_cannot_enter_memory`, `test_secret_material_cannot_enter_memory` |
| F-08 | MEDIUM | CI | Actions pinned by mutable tag (`@v4`, `@v5`) | `.github/workflows/ci.yml:17-27` | REQUIRED_FOR_CICD: pin by SHA |
| F-09 | MEDIUM | Dependencies | `pip install -c requirements.lock` is not hash-checked | `ci.yml:20` | REQUIRED_FOR_CICD: `--require-hashes` with a hashed lock |
| F-10 | LOW | Worker shell | `target` starting with `-` becomes an option (`--fix`, `-p<plugin>`). The production caller passes no target today | `worker/command.py:80-84` | FIXED (cc13fa0): a leading-dash path is passed as `./<path>` (`worker/command.py:86`) |
| F-11 | LOW | Staging | No per-actor quota; identical bytes stored once per upload; no purge | `staging.py:154-176` | REQUIRED_FOR_LIFECYCLE: content-addressed blob with refcount, quota, R1 purge |
| F-12 | LOW | Remote auth | No rate limit or lockout on failed proofs | `routes.py:59-65` | Recommend a per-actor/IP failure budget (429) and alerting |
| F-13 | LOW | Remote auth | Proof hashed with unsalted SHA-256; a weak operator-chosen proof is offline-guessable from the config | `auth.py:95`, `control/security.py:13` | ACCEPTED_RISK when proofs are generated with at least 128 bits of entropy; recommend a generator command and documentation |
| F-14 | LOW | Remote | `/nodes` and `/mission-control` are not project-scoped | `routes.py` `node_status`; `app.py:62` | Recommend filtering by `registration.project_ids ∩ actor.projects` |
| F-15 | LOW | Remote ledger | `threading.Lock` only: multi-process appends can fork the hash chain. The in-flight guard is per process | `ledger.py:22-40` | FIXED (cc13fa0: OS file lock; CR-05: persisted claims `ledger.py:159`, `service.py:183`) |
| F-16 | INFO | Remote ledger | Truncating the ledger tail is undetectable and makes an intent_id re-dispatchable (requires host file write, out of scope per #4) | `ledger.py:55` | Recommend a periodic anchored head (audit chain) |
| F-17 | LOW | Compute node | Link-local (incl. `169.254.169.254` metadata) is accepted as LAN by design | `config.py:104` | ACCEPTED_RISK (operator-authored registry, fixed path `/capabilities`); recommend denying the metadata IPs |
| F-18 | LOW | Memory | `record_decision` and `register_markdown_source` use the weaker `assert_no_secret` | `memory/service.py:151`, `memory/service.py:727` | REQUIRED_FOR_LIFECYCLE: call `assert_memory_content` (outside allowlist) |
| F-19 | LOW | Lifecycle/remote | (a) Ledger `context=` field trips the prohibited-name rule. Verified: it is the fingerprint summary only, allow-listed by name. (b) READ outcomes ledgered compact rows incl. goal text | `service.py:137`, `service.py:95` | (a) VERIFIED by `test_ledgered_context_is_the_fingerprint_summary_only`; (b) FIXED by `test_status_reads_do_not_copy_projection_rows_into_the_intent_ledger` |
| F-20 | LOW | Product loop | `_run_context_payload` persists `bounded_context` without `assert_minimized` | `product_loop/service.py:652` | REQUIRED_FOR_LIFECYCLE / product-loop owner (outside allowlist) |
| F-21 | MEDIUM | Backup | Plaintext dump, no hash, one DB only, no object store, default-credential fallback | `scripts/backup.ps1:20-46` | REQUIRED_FOR_LIFECYCLE / ops: encrypt, hash, cover both lanes and the object store, fail without an explicit credential |
| F-22 | LOW | Connectivity | Future snapshot reported fresh; a naive timestamp raises TypeError | `connectivity/config.py:91` | REQUIRED_FOR_CONNECTIVITY: same rule as `compute_node/visibility.py:52` |
| F-23 | LOW | Compute node | Private-host check used `ipaddress.is_private`, which admits 6to4/Teredo/NAT64 and reserved ranges and varies by CPython patch | pre-fix `config.py:97-110` | FIXED: explicit allowlist (`config.py:104`); `test_private_host_check_is_an_explicit_allowlist` |
| F-24 | LOW | Compute node | Node error detail persisted verbatim (credential echo); an over-long reason crashed probes and routing | pre-fix `registry.py:162` | FIXED: `_error_code` (`registry.py:172`) |
| F-25 | LOW | Compute node | Future/naive probe timestamps were treated as fresh or crashed visibility | pre-fix `visibility.py:50-56` | FIXED (`visibility.py:52`) |
| F-26 | LOW | Remote | Concurrent same-`intent_id` requests could both dispatch when the port awaits (latent: current ports do not yield) | pre-fix `service.py:136-142` | FIXED: `INTENT_IN_FLIGHT` |
| F-27 | LOW | Remote auth | Actor registry silently ignored plaintext credential fields | pre-fix `auth.py:52-66` | FIXED: `REMOTE_PLAINTEXT_CREDENTIAL_FORBIDDEN` |
| F-28 | MEDIUM | Enterprise | Revoked text was not part of `unauthorized_texts`, so the output leak guard passed copies of revoked evidence | pre-fix `pipeline.py:228-232` | FIXED (`pipeline.py:228`); `test_revoked_text_is_part_of_the_output_leak_guard` |
| F-29 | LOW | Enterprise | `HttpReranker` accepts any endpoint (no private/userinfo check); not constructed from config today | `rerank.py:105-120` | Recommend validation at the config-binding point (a constructor check would change accepted tests) |
| F-30 | INFO | Repo | `ruff check src tests` fails at base on `tests/consolidation/test_phase2_context_resolver.py:150` (E731) | ruff output | Owner of that test |

### Fixer round 1: independent review CR-05 (all ten findings accepted)

| CR-05 | Sev | Fix (file:line) | Regression test (`tests/security/test_hardening_cr05.py`) |
|---|---|---|---|
| F-05 | HIGH | `scope["server"]` is the connection's local address, not the listener bind. Guarded launcher `remote/serve.py:31` (`bind_listener`) and `:75` (`serve`); attestation `deployment.py:114`; request check `deployment.py:145`, `routes.py:57`. A plain uvicorn launch is loopback-only | `test_cr05_plain_uvicorn_launch_never_serves_the_overlay`, `test_cr05_launcher_refuses_*`, `test_cr05_attested_launcher_serves_only_its_verified_socket`, `test_cr05_attestation_requires_*`, `test_cr05_real_launcher_socket_end_to_end_on_loopback` |
| INTENT-DUPLICATE | HIGH | Persisted `CLAIMED` record under the ledger lock before dispatch (`ledger.py:159`), checked by `service.py:183`. Released only when refused before any authority port is called (`service.py:195`). A claim with no outcome past its lease returns `INTENT_CLAIM_UNRESOLVED` | `test_cr05_two_instances_sharing_a_ledger_dispatch_an_intent_once`, `test_cr05_claim_is_atomic_across_processes`, `test_cr05_claim_left_by_a_crash_*`, `test_cr05_port_failure_after_dispatch_keeps_the_claim`, `test_cr05_refusal_before_dispatch_releases_the_claim` |
| INBOX-OPEN | MEDIUM | `O_NONBLOCK\|O_NOFOLLOW` open, fstat and inode check, then a bounded copy into a PADOS-owned snapshot. A size/mtime change refuses the file (`staging.py:237`) | `test_cr05_fifo_swapped_in_*`, `test_cr05_inbox_file_modified_while_copied_is_refused`, `test_cr05_inbox_registers_the_snapshot_*` |
| SANDBOX-WINDOWS | MEDIUM | Trailing dot/space refused; device stem checked after normalization (`adapter.py:38`); case-insensitive collisions across inputs and outputs (`adapter.py:51`) | `test_cr05_windows_normalized_*`, `test_cr05_case_insensitive_collisions_*` |
| UPLOAD-BUFFER | MEDIUM | Size checked before the chunk is copied (`routes.py:33`) | `test_cr05_single_oversized_chunk_*` |
| NODE-REASON-LEAK | MEDIUM | Descriptor reasons mapped to local codes plus descriptor-validated identifiers; the persisted descriptor carries the mapped reasons; only the code is logged (`registry.py:154`) | `test_cr05_descriptor_reasons_are_mapped_to_local_codes` |
| NODE-ERROR-SCHEMA | MEDIUM | An error body must be an object with a short string `detail` (`registry.py:136`); a success body must be an object (`registry.py:206`) | `test_cr05_malformed_node_responses_become_local_error_codes` |
| BACKEND-URL | LOW | `private_http_url` parses with `httpx.URL` (scheme, userinfo, query/fragment, port, allowlisted host) and returns the parsed form the client uses (`config.py:132`). Also used for registrations | `test_cr05_backend_endpoints_*` |
| LEDGER-LOCK-PATH | MEDIUM | No-follow opens with a regular-file check (`ledger.py:61`); lock is 0600, owned by the uid, `nlink==1` (`ledger.py:120-140`); world-writable non-sticky directory refused (`ledger.py:98`). Protected directory documented in `docs/remote/REMOTE_OPERATIONS_V1.md` | `test_cr05_ledger_and_lock_links_are_refused`, `test_cr05_lock_is_private_and_unreplaceable` |
| ACTOR-PLAINTEXT | LOW | Recursive plaintext check over the whole config, with project-id-keyed maps treated as data (`auth.py:45`); closed actor-row schema (`auth.py:57`) | `test_cr05_nested_plaintext_*`, `test_cr05_actor_rows_have_a_closed_schema`, `test_cr05_project_ids_used_as_keys_*` |

Residual. Probe freshness tolerates up to 60 s of forward clock skew (`visibility.py:52`); only
timestamps further in the future count as stale. On Windows the inbox has no `O_NOFOLLOW` or
`O_NONBLOCK`, so it relies on the lstat, inode and size/mtime checks. The ledger lock owner check is
POSIX-only.
