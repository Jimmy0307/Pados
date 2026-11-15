# PADOS Unified Identity & Session: security tests

Each requested scenario below is mapped to its automated proof. **Unit and integration** tests are FastAPI TestClient
tests under `tests/identity/` and run in CI on Linux, Windows and macOS. **E2E** steps come from
`scripts/dogfood/identity_e2e.py`, which uses real processes: `pados init`, a private PostgreSQL, `pados serve`, the
CLI, HTTP and PowerShell 5.1. **Browser** means the live Playwright spec `web/e2e/identity-live.spec.ts`.

## Requested scenarios

| Scenario | Unit / integration | E2E (real server and PostgreSQL) |
| --- | --- | --- |
| First pairing | `test_pairing.py::test_pair_on_loopback_http_sets_the_plain_httponly_strict_cookie`, `::test_a_code_works_once` | `pairing_code_cli_json`, `pair_with_allowlisted_origin`, `pairing_code_single_use`; browser spec |
| Reload without entering the token | `test_sessions.py::test_reload_restores_the_session_with_the_same_csrf` | `reload_restores_session`, `session_survives_server_restart`; browser spec (reload, then "Paired as …", no token box) |
| A paired user creates a Work Request | `test_control_routes.py::test_session_creates_a_work_request_with_csrf_and_origin` | `work_request_via_session_only`, `task_loop_and_result_candidate_via_session_only`; browser spec |
| Logout | `test_sessions.py::test_logout_revokes_only_the_current_session`, `::test_logout_with_a_dead_session_clears_the_cookie` | `logout_revokes_current_session`; browser spec |
| Revocation | `test_sessions.py::test_revoking_a_device_revokes_its_sessions`, `::test_a_session_may_revoke_its_own_device_with_csrf` | `device_revoke_kills_other_devices_session`, `cli_device_list_and_revoke` |
| Expiry | `test_sessions.py::test_absolute_expiry`, `::test_idle_timeout_slides_on_use`; `test_pairing.py::test_an_expired_code_is_refused_with_expired` | `idle_expiry` (a real 130 s wait) |
| Cross-account | `test_oidc_actor_authority.py::test_cross_account_a_session_cannot_act_as_another_actor`, `::test_device_and_actor_sessions_coexist_without_mixing`; `test_control_routes.py::test_csrf_of_another_session_is_refused` | `device_revoke_kills_other_devices_session` |
| Cross-project | `test_oidc_actor_authority.py::test_contributor_on_a_cannot_create_in_b` | — (the device session has control-token scope by design) |
| CSRF | `test_control_routes.py::test_session_writes_are_refused_without_every_rule`, `::test_csrf_of_another_session_is_refused`; `test_oidc_actor_authority.py::test_actor_writes_need_csrf_and_origin` | `refusals_with_closed_codes` |
| Wrong Origin | `test_pairing.py::test_pair_requires_an_allowlisted_origin`, `::test_pair_refuses_a_cross_site_fetch`; `test_sessions.py::test_session_from_a_foreign_origin_is_refused`; `test_oidc_flow.py::test_a_foreign_page_cannot_start_a_sign_in` | `pair_refuses_wrong_origin`, `refusals_with_closed_codes` |
| Forged token or cookie | `test_sessions.py::test_forged_and_malformed_cookies_are_refused`; `test_control_routes.py::test_an_invalid_token_header_is_refused_even_with_a_valid_cookie`; `test_oidc_flow.py::test_signature_attacks_are_refused`; `test_oidc_jws.py` | `refusals_with_closed_codes` (forged cookie, invalid token header next to a valid cookie) |
| OAuth replay | `test_oidc_flow.py::test_code_and_state_replay_is_refused`, `::test_state_from_another_browser_is_refused_and_not_consumed`, `::test_expired_state_is_refused` | `oidc_configured_provider_fails_closed` (forged callback refused). Live sign-in: **NOT_LIVE_VALIDATED** |
| WebAuthn cannot be bypassed | `test_control_routes.py::test_a_session_cannot_decide_a_result_candidate_approval`; `test_oidc_actor_authority.py::test_an_oidc_session_cannot_decide_a_result_acceptance_approval` | `step_up_cannot_be_bypassed_by_session` (session and token both get `RESULT_ACCEPTANCE_REQUIRES_STEP_UP`; the approval stays PENDING) |
| Windows PowerShell 5.1 | `test_cli_device.py::test_windows_powershell_51_parses_the_json` | `pairing_code_powershell51`, `pair_with_powershell_minted_code` |

## Further properties

| Property | Proof |
| --- | --- |
| Session fixation | `test_sessions.py::test_fixation_a_preset_cookie_is_never_adopted`, `::test_pairing_with_a_valid_cookie_revokes_it_and_issues_a_new_id` |
| Cookie attributes for HTTP and HTTPS | `test_pairing.py::test_pair_on_loopback_http_sets_the_plain_httponly_strict_cookie`, `::test_pair_over_https_sets_the_host_prefixed_secure_cookie`, `::test_cookie_policy_never_is_refused_over_https`, `::test_forwarded_proto_never_selects_the_cookie_variant` |
| Flag off means behaviour unchanged | `test_control_routes.py::test_flag_off_ignores_the_cookie_and_answers_exactly_as_before`; `test_pairing.py::test_disabled_identity_refuses_every_route_but_session` |
| Token rotation ends sessions | `test_sessions.py::test_rotating_the_control_token_ends_every_session`; E2E `control_token_rotation_ends_sessions` |
| No secrets in logs, responses or storage | `test_secret_hygiene.py`; `test_oidc_secret_hygiene.py`; E2E `no_control_token_or_session_secret_leak` (63 responses and all logs scanned); web tests on storage and console |
| Unconfigured SSO is hidden and refused | `test_oidc_config.py::test_unconfigured_providers_are_hidden_and_refused`, `::test_the_feature_flag_gates_everything`; E2E `oidc_configured_provider_fails_closed` |
| Pairing lockout resistance | `test_pairing.py::test_failed_attempts_never_burn_the_operators_code`, `::test_a_code_is_single_use_under_concurrent_pairing` |
| OIDC flood and outage handling | `test_oidc_hardening.py` (6 tests) |
| Store integrity | `test_store.py` (8 tests) |
| Route inventory (OP-004) | `tests/operability/test_op004_mutation_inventory.py` (it caught the unclassified actor route during E2E, fixed in `298d32f`) |

## Results (2026-11-16)

| Run | Result |
| --- | --- |
| `tests/identity`, Windows (head `2bdbd97`) | 236 passed, 2 skipped (platform) |
| Identity E2E, Windows 11 (head `eaeb9f4`, same identity source) | 26 PASS, 0 FAIL, 1 NOT_LIVE_VALIDATED (OIDC live sign-in) |
| Live Playwright, Windows | 1 passed. Mocked specs: 24 passed, 6 skipped (they need a live environment). |
| Linux (VMware Ubuntu 24.04) chain and E2E | See the PR body for the run at `298d32f` |
