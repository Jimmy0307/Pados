# PADOS Unified Identity & Session v1: release notes

Branch `feat/unified-identity-session-20261116`, based on `main` `9b975a9`. **Not merged; it needs owner review.**

## What changes for the operator

- Run `pados device pair` once and type the code into **Pair this browser** in the Control Center. After that, reloads
  restore the session, and Work Requests and other control actions need no control token.
- The Control Center shows:
  - pairing status ("Paired as …");
  - a device list with revoke;
  - **Log out**;
  - an explanation whenever the page cannot proceed (identity disabled, session expired, or a WebAuthn step-up is
    required).
- Result acceptance is unchanged. It still needs the Remote console's WebAuthn step-up. A session never accepts a
  result.

## Changelog

- **Phase 1:**
  - `src/pados/identity/`: one-time pairing codes; server-side device sessions in an HttpOnly cookie, with Origin,
    CSRF and Fetch-Metadata checks and session-fixation defence; expiry, idle timeout, logout, revoke and a device
    list; an owner-only JSON store of hashes.
  - The `control_auth` dependency accepts a session only when no control-token header is present.
  - `pados device pair|list|revoke`.
  - `pados init` writes `PADOS_IDENTITY_STORE_DIR` and `PADOS_IDENTITY_SESSIONS_ENABLED=false`.
  - `pados serve` disables proxy headers and the access log when identity is on.
- **Web:**
  - `identity.ts`, `control-auth.ts`, `identity-panel.tsx` and the Vite `/identity` proxy.
  - The token-in-memory path is kept as the fallback.
- **Phase 2:**
  - `src/pados/identity/oidc/`: an OIDC relying party for Microsoft Entra ID, Google and generic providers, using
    Authorization Code with PKCE S256, state and nonce, and pure-Python RS256 ID-token verification.
  - PADOS actors with `viewer`/`contributor`/`approver` project roles (`src/pados/identity/actors.py`).
  - The actor surface `GET /identity/v1/me` and `POST /identity/v1/projects/{id}/work-requests` (contributor only),
    through the shared creation function.
- **Reviews:** two independent security reviews, of phase 1 and of phase 2, both MERGE_OK. Their P2 items are fixed
  with tests: pairing lockout, single-use under concurrency, dead-session cookie, OIDC start flooding, discovery
  cooldown, and closed codes for malformed provider input. The Windows E2E run also found an unclassified mutating
  route (OP-004 inventory), fixed in `298d32f`.
- **E2E:** `scripts/dogfood/identity_e2e.py` and `web/e2e/identity-live.spec.ts`.
- **Security closure (A2–A4):**
  - **A2, least privilege:** a paired browser session may call only the allowlisted Control Center routes
    (`src/pados/identity/scopes.py`); everything else, including `/context/assemble`, memory writes and
    projection rendering, is `SESSION_SCOPE_DENIED`. The control token keeps full authority. Design §3.8 has the
    route table.
  - **A3, store protection:** on Windows the store directory is created with an explicit protected DACL (token
    user SID + SYSTEM, inherited by `sessions.json` and `server.key`) and every load refuses a store open to
    another SID with `IDENTITY_STORE_INSECURE`; POSIX keeps 0700/0600 and gains the same check. Sessions off
    loopback refuse unless the `__Host-` Secure cookie is forced, and the loopback-binding rule still refuses.
    Design §3.9.
  - **A4, SSO UI:** a 單一登入 (SSO) section on **This browser & devices**: provider sign-in links, zh-TW return
    messages with the closed code in a detail (the address bar is cleaned), the signed-in actor with per-project
    roles, and an actor-only logout (`POST /identity/v1/actor/logout`, new). Design §3.10.
  - **Upgrade note:** an existing store directory that is readable by another account is now refused. Delete it
    (every session is revoked, as in rollback step 2) and PADOS recreates it protected.
- **No change:** no new dependency, no database migration, and no change to the scheduler, router, CAS, ScopeVersion,
  ResultCandidate, WebAuthn step-up or `/remote/v1`.

## Feature flags (all off by default)

| Setting | Default | Effect |
| --- | --- | --- |
| `PADOS_IDENTITY_SESSIONS_ENABLED` | `false` | Off: cookies are ignored; `/identity/v1/*` answers `IDENTITY_DISABLED` (`GET /session` answers `enabled:false`); the UI keeps the token box. |
| `PADOS_IDENTITY_OIDC_CONFIG` | unset | Unset: no SSO providers are shown, and OIDC routes answer `OIDC_PROVIDER_NOT_CONFIGURED`. |
| `PADOS_IDENTITY_ACTORS_CONFIG` | unset | Unset: no identity is bound, so a successful SSO login gets no session. |
| `PADOS_IDENTITY_SESSION_TTL_SECONDS` / `PADOS_IDENTITY_IDLE_TIMEOUT_SECONDS` | 86400 / 7200 | Absolute and idle lifetimes |
| `PADOS_IDENTITY_ALLOWED_ORIGINS` | loopback 5173 and 8088 | Origins allowed on cookie-authenticated requests |
| `PADOS_IDENTITY_COOKIE_SECURE` | `auto` | `auto` picks the cookie variant from the request scheme. Forwarded headers are never trusted. |

## Rollback

1. Set `PADOS_IDENTITY_SESSIONS_ENABLED=false` and restart `pados serve`. Every session stops working immediately and
   the token path is exactly as before.
2. To also revoke everything that was issued, delete `<PADOS_IDENTITY_STORE_DIR>/sessions.json`, or rotate the
   control token with `pados init --force`, which ends every session.
3. To remove the code, revert the merge commit through a PR. No database migration is involved.

## Validated, and not

| Item | Status |
| --- | --- |
| Device pairing, sessions, revoke, logout, expiry, CSRF, Origin, fixation, rotation | **Validated with a real runtime** on Windows 11 (identity E2E with real PostgreSQL, CLI, HTTP and PowerShell 5.1) and a live browser (Playwright). Linux: see the PR. |
| Work Request creation with only a paired session | **Validated with a real runtime** (E2E `work_request_via_session_only`, browser spec) |
| WebAuthn step-up cannot be bypassed by a session | **Validated with a real runtime** (E2E `step_up_cannot_be_bypassed_by_session`) |
| OIDC flow, ID-token validation, actor roles, cross-project, replay | **Validated synthetic** (a fake issuer with real RS256 signatures; integration tests) |
| Microsoft Entra ID with a real tenant | **NOT_LIVE_VALIDATED** |
| Google with a real client | **NOT_LIVE_VALIDATED** |
| OIDC through an HTTP proxy | Not supported (`trust_env=False`) |
| SSO sign-in section, return messages, actor view and actor logout in the Control Center | **Validated synthetic** (Vitest and mocked Playwright). Real providers remain NOT_LIVE_VALIDATED. |
| Session least privilege (`SESSION_SCOPE_DENIED`) | **Validated synthetic** (integration tests over every `control_auth` route). Not yet re-run in the real-runtime E2E. |
| Windows store DACL from creation and fail-closed check | **Validated on Windows 11** with real ACLs (`icacls`, SIDs) in the integration tests. Not a Windows user authentication. |
| Windows Hello as an identity factor; trusted Windows SID binding | **NOT_IMPLEMENTED** (design §5). The SID must come from an OS-attested named-pipe client token, never from a username, a header or browser JS. |
| `approver` role | Reported by `/me`; it grants nothing yet. Approvals and result acceptance stay on their existing paths. |
| Multi-process server | Not supported (the JSON store is per process). `pados serve` runs one worker. |
| Windows full validation (`windows_validate.py --profile full`) at this head | Partially run. Every step through Pester passed except the full suite (163 baseline failures plus 4 environment-only DI cases; the OP-004 gap was fixed). The run was stopped for low host memory before its two Playwright steps, which then passed separately. |
