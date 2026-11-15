# PADOS Unified Identity & Session: threat model

Scope:

- **Phase 1:** device pairing and server-side sessions for the Local Control Center.
- **Phase 2:** OIDC sign-in with PADOS actor and project-role mapping.

Method: STRIDE over the assets and trust boundaries below. Every control names the test that proves it. Test files
are under `tests/identity/`, and E2E steps come from `scripts/dogfood/identity_e2e.py`. The full inventory is in
[`SECURITY_TESTS.md`](SECURITY_TESTS.md).

## Assets

| Asset | Where | Sensitivity |
| --- | --- | --- |
| Control token | `runtime/local/control.token`; its hash is in `.env` | Full local control-plane authority |
| Pairing code | CLI output, typed once into the browser | Becomes a session within 5 minutes |
| Device session id | HttpOnly cookie `pados_session` (or `__Host-pados_session`); SHA-256 in the store | Control-plane authority until expiry or revocation |
| Actor session id (OIDC) | HttpOnly cookie `pados_actor`; SHA-256 in the store | Only the role-gated actor surface |
| CSRF value | Page memory; HMAC of the session id under the server key, never stored | Needed for every state change |
| Server key | `<store>/server.key` | Derives CSRF values and the control-token binding |
| Session store | `<store>/sessions.json` (hashes only) | Device, session and OIDC state validity |
| OIDC client secret | An environment variable named by the config (the config holds only a reference) | Exchanges authorization codes |
| OIDC state, nonce, PKCE verifier | State hashed in the store; nonce and verifier HMAC-derived from the state | Login integrity |
| Actor bindings and project roles | `PADOS_IDENTITY_ACTORS_CONFIG` | Who may view, contribute or approve |
| WebAuthn presence keys and step-up proofs | Existing remote store (unchanged) | Result acceptance |

## Trust boundaries

1. **Browser page and the loopback API.** The page is same origin through the Vite proxy or the API's own origin.
   Everything the browser sends is untrusted input.
2. **Other local processes and users and the loopback API.** Any local process can reach loopback, so **loopback is
   not identity**.
3. **CLI and API.** The CLI proves possession of the control token, which needs OS file access.
4. **API and OIDC provider.** Traffic is HTTPS only. Discovery documents, JWKS and the token endpoint are untrusted
   until validated.
5. **Session layer and Core authority.** No session enters the step-up, CAS or ScopeVersion chain.

## STRIDE

| # | Threat | Control | Proof |
| --- | --- | --- | --- |
| S1 | Another local process or user calls `/control` as the operator | Needs the control token or a paired session; loopback alone grants nothing. Pairing needs a code minted with the token. | `test_control_routes.py::test_session_writes_are_refused_without_every_rule`; E2E `refusals_with_closed_codes` |
| S2 | Pairing code brute force | About 50-bit code, 5-minute life, at most 30 failures per 60 s (about 150 guesses per code) | `test_pairing.py::test_the_global_failure_limit_pauses_pairing_briefly`, `::test_wrong_and_unknown_codes_are_indistinguishable` |
| S3 | Session fixation | The server only issues ids itself. A preset cookie is never adopted. `/pair` revokes a presented session and issues a new id. | `test_sessions.py::test_fixation_a_preset_cookie_is_never_adopted`, `::test_pairing_with_a_valid_cookie_revokes_it_and_issues_a_new_id` |
| S4 | Stolen session cookie | HttpOnly; absolute and idle expiry; per-device revoke; logout; a session cannot mint pairing codes | `test_pairing.py::test_a_session_cookie_cannot_mint_pairing_codes`; `test_sessions.py::test_absolute_expiry`, `::test_idle_timeout_slides_on_use`, `::test_revoking_a_device_revokes_its_sessions`; E2E `idle_expiry`, `device_revoke_kills_other_devices_session` |
| S5 | OIDC identity confusion (reused email, mixed tenants) | Identity is `(issuer, subject)`; exact `iss` and `aud`; Entra tenant pinned, or `tid` restricted to an allowlist | `test_oidc_flow.py::test_same_email_with_another_subject_is_not_bound`, `::test_entra_multi_tenant_accepts_only_pinned_tids`, `::test_invalid_claims_are_refused` |
| S6 | Forged ID token | RS256 against the provider JWKS; `alg` pinned; `jwk`, `jku`, `x5u`, `x5c` and `crit` refused; the exact EMSA-PKCS1-v1_5 encoding is compared | `test_oidc_jws.py::test_only_the_exact_emsa_encoding_verifies`, `::test_headers_are_pinned`; `test_oidc_flow.py::test_signature_attacks_are_refused` |
| T1 | CSRF against writes | SameSite=Strict cookie; `Origin` allowlist; `X-PADOS-CSRF`; `Sec-Fetch-Site` check | `test_control_routes.py::test_session_writes_are_refused_without_every_rule`, `::test_csrf_of_another_session_is_refused`; E2E `refusals_with_closed_codes` |
| T2 | Tampered session store | Owner-only file with atomic replace; hashes only; a corrupt file fails closed; a linked file is refused | `test_store.py::test_corruption_fails_closed`, `::test_a_linked_store_document_is_refused`, `::test_a_damaged_server_key_fails_closed` |
| T3 | OAuth code or state replay or injection | Single-use state bound to a pre-auth cookie and to the provider; PKCE S256; nonce | `test_oidc_flow.py::test_code_and_state_replay_is_refused`, `::test_state_from_another_browser_is_refused_and_not_consumed`, `::test_pkce_verifier_mismatch_is_refused` |
| T4 | Forwarded headers flip HTTP to HTTPS (cookie variant) | `proxy_headers=False`; `X-Forwarded-*` ignored | `test_pairing.py::test_forwarded_proto_never_selects_the_cookie_variant`; `test_init_and_serve.py::test_serve_disables_proxy_headers_only_with_identity_on` |
| R1 | Unattributable actions | Session-authenticated requests log `device_id` or `actor_id`, never secrets | `test_secret_hygiene.py::test_secrets_never_reach_logs_or_other_responses` |
| I1 | Token or secret leakage | The raw control token never appears in a response, log, URL or browser storage. Codes, session ids and CSRF values are never logged. The OIDC client secret is held only by reference. | `test_secret_hygiene.py` (both tests); `test_oidc_secret_hygiene.py`; E2E `no_control_token_or_session_secret_leak`; web tests (no storage, no console) |
| I2 | Cleartext cookie beyond loopback | `__Host-` cookie with `Secure` on HTTPS; plain cookie only on loopback HTTP | `test_pairing.py::test_pair_over_https_sets_the_host_prefixed_secure_cookie`, `::test_pair_on_loopback_http_sets_the_plain_httponly_strict_cookie`, `::test_cookie_policy_never_is_refused_over_https` |
| D1 | Flooding pairing or OIDC start | Pairing pause clears in 60 s; failures never burn codes. OIDC start limited to 10 per minute, at most 32 pending, never evicted. Discovery failures cool down. | `test_pairing.py::test_failed_attempts_never_burn_the_operators_code`; `test_oidc_hardening.py` (all) |
| E1 | A device session gains more than control-token authority | Same dependency and the same routes as the token; nothing on `/remote/v1` or graph-run | `test_control_routes.py::test_a_session_grants_nothing_on_the_remote_actor_surface`, `::test_graph_runs_still_require_the_control_token_header`, `::test_flag_off_ignores_the_cookie_and_answers_exactly_as_before` |
| E2 | A session or SSO login performs result acceptance | `decide_approval` still refuses (`RESULT_ACCEPTANCE_REQUIRES_STEP_UP`); step-up needs a WebAuthn proof bound to the candidate and ScopeVersion | `test_control_routes.py::test_a_session_cannot_decide_a_result_candidate_approval`; `test_oidc_actor_authority.py::test_an_oidc_session_cannot_decide_a_result_acceptance_approval`; E2E `step_up_cannot_be_bypassed_by_session` (real PostgreSQL; the approval stays PENDING) |
| E3 | SSO success implies admin, project access or approval | Unbound means nothing. Only `contributor` grants anything (work-request creation). No role grants control-token authority. | `test_oidc_flow.py::test_unbound_identity_gets_no_session`; `test_oidc_actor_authority.py::test_viewer_cannot_create`, `::test_approver_role_grants_no_creation`, `::test_an_oidc_session_is_refused_by_every_control_route`, `::test_an_oidc_session_has_no_device_or_pairing_authority` |
| E4 | Cross-project or cross-account action | Per-project role check; the actor id comes from the session, never the body; idempotency keys namespaced per actor | `test_oidc_actor_authority.py::test_contributor_on_a_cannot_create_in_b`, `::test_cross_account_a_session_cannot_act_as_another_actor` |
| E5 | Token rotation leaves old sessions alive | Sessions bind an HMAC of the control-token hash | `test_sessions.py::test_rotating_the_control_token_ends_every_session`; `test_oidc_flow.py::test_rotating_the_control_token_ends_actor_sessions`; E2E `control_token_rotation_ends_sessions` |

## Accepted or deferred risks

| Risk | Disposition |
| --- | --- |
| Local malware running as the operator can read the control token or the browser cookie database | Out of scope for the single-user local profile. Mitigation would be the phase 3 Windows SID channel or an OS keychain; NOT_IMPLEMENTED. |
| A local process can keep pairing or OIDC start paused (availability only) | Accepted. The control-token path is unaffected. |
| Over loopback HTTP, cookies are not `Secure` and not port-isolated (another localhost app receives them and could plant a pre-auth cookie for login CSRF) | Use HTTPS beyond a single-user host. Documented in `OIDC_SETUP.md` §7. |
| The JSON store is safe for one server process only | `pados serve` runs one worker. A DB-backed store is the multi-process option. |
| Windows store files rely on the inherited per-user ACL of `runtime/local` (as `control.token` does), not an explicit ACL | Accepted for local use; an explicit ACL is a follow-up. |
| Real Microsoft Entra ID and Google | **NOT_LIVE_VALIDATED.** Tests use a fake issuer only. |
| Windows Hello as an identity factor and binding to a trusted Windows SID | **NOT_IMPLEMENTED.** Design §5 (phase 3). |
