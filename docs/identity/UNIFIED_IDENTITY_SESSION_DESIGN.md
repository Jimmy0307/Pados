# PADOS Unified Identity & Session: design and contract (v1)

Status: **in development** on `feat/unified-identity-session-20261116`. Feature flag: `PADOS_IDENTITY_SESSIONS_ENABLED`
(default `false`). Nothing here changes the scheduler, router, CAS, ScopeVersion, ResultCandidate or WebAuthn step-up
authority chain.

## 1. Problem and goal

Every load of the Local Control Center requires pasting `runtime/local/control.token` into the page. Goal of phase 1:
**pair a browser once**, after which reloads restore a server-side session. No raw control token reaches the browser
(code, `localStorage`, `sessionStorage`, URL, logs or any downloadable response), and no permission boundary is
widened.

## 2. Principles

1. **The trust anchor stays the control token.** Only a caller holding the control token (the operator who can read
   `runtime/local/control.token`) can mint a pairing code. Pairing converts one proof of that possession into a
   device session. A session never grants more than the control token. Since the security closure (§3.8) it grants
   **less**: only the explicitly allowlisted Control Center routes.
2. **Loopback is not identity.** The API binding to 127.0.0.1 remains a precondition, but pairing always needs a code.
3. **Sessions are server-side.** The browser holds only an opaque, random, HttpOnly cookie. The server stores only
   hashes.
4. **Sensitive decisions are unchanged.** A session cannot decide a ResultCandidate approval
   (`RESULT_ACCEPTANCE_REQUIRES_STEP_UP` still applies). The remote `/remote/v1` actor model and WebAuthn step-up
   are untouched. An SSO session (phase 2) never substitutes for step-up.
5. **Fail closed, closed codes, no secrets in logs.** Log device ids, never codes, session ids or CSRF tokens.

## 3. Phase 1: device pairing and sessions

### 3.1 Flow

1. The operator runs `pados device pair`. The CLI reads the control token file locally and calls
   `POST /identity/v1/pairing-codes` with `X-PADOS-Control-Token`. It prints a one-time pairing code.
2. In the Control Center the operator chooses **Pair this browser** and types the code.
3. The browser calls `POST /identity/v1/pair` with the code. The server consumes the code and creates a device and a
   session. It sets an HttpOnly session cookie and returns the CSRF token in the JSON body.
4. On reload the page calls `GET /identity/v1/session`. A valid cookie restores the session, and the response
   carries the CSRF token again (derived, not stored).
5. Writes to `/control/*` send the cookie (automatically) plus `X-PADOS-CSRF` and an allowlisted `Origin`.

### 3.2 Pairing codes

- 10 characters of Crockford base32 (about 50 bits), shown grouped as `XXXXX-XXXXX`.
- Stored only as SHA-256. TTL 300 s. Single use.
- At most 3 outstanding codes.
- A global limit of 30 failed pair attempts per 60 seconds, after which pairing pauses with `PAIRING_RATE_LIMITED`
  until the window clears. Failed attempts never burn a code (review P2-1): `/pair` is unauthenticated, so burning
  would let any local process destroy the operator's code. At most about 150 guesses fit in a code's life
  against 2^50 codes. Residual: a process that keeps flooding can delay pairing (availability only).
- Minting a code requires the control token (header) and the feature flag. It is refused with a session cookie
  alone: a session cannot mint pairing codes, so a stolen session cannot pair more devices.

### 3.3 Sessions and devices

- **Session id:** 32 random bytes (`secrets.token_urlsafe(32)`). Only `SHA-256(session id)` is stored.
- **CSRF value:** the derived CSRF value is `HMAC-SHA256(server_key, "csrf:" + session_id)`, URL-safe base64. It is
  never stored; it is recomputed on demand. The server key is 32 random bytes in `<store>/server.key` (owner-only,
  created atomically).
- **Control-token binding:** each session records `HMAC-SHA256(server_key, control-token hash)` at pairing. Rotating
  the control token (`pados init --force`) therefore ends every session (`SESSION_REVOKED`).
- **Lifetimes:**
  - absolute TTL `PADOS_IDENTITY_SESSION_TTL_SECONDS` (default 86400);
  - idle timeout `PADOS_IDENTITY_IDLE_TIMEOUT_SECONDS` (default 7200), sliding on authenticated use;
  - `last_seen` writes are throttled to once per 60 s.
- **Device:** a public random `device_id` (`dev_` + 16 hex), a label (1–64 printable characters, sanitized),
  `paired_at`, `last_seen_at`, `revoked_at`. Revoking a device revokes all its sessions. Logout revokes only the
  current session.
- **Limits:** at most 16 active devices and 4 active sessions per device. Beyond that the oldest is revoked.
- **Store:**
  - `PADOS_IDENTITY_STORE_DIR`, default `runtime/local/identity`. `pados init` writes the absolute path.
  - The file is JSON with a schema version and only hashes. It is written owner-only through an atomic replace,
    never through an existing file, with a process-wide lock.
  - It survives a server restart. Corruption fails closed (no session is valid) and is logged by code.
- **Fixation:**
  - The server only ever issues session ids itself.
  - A cookie presented to `/pair` is revoked if it is valid and ignored otherwise; a new id is always issued.
  - Ids are never accepted from the client except as a cookie lookup.

### 3.4 Cookie and request validation

| Context | Cookie |
| --- | --- |
| Loopback HTTP (`http://127.0.0.1`, `http://localhost`) | `pados_session=<id>; HttpOnly; SameSite=Strict; Path=/` (no `Secure`, which browsers would drop over HTTP) |
| HTTPS | `__Host-pados_session=<id>; HttpOnly; Secure; SameSite=Strict; Path=/` (no `Domain`) |

- **Choosing the variant:** `PADOS_IDENTITY_COOKIE_SECURE=auto|always|never` (default `auto`, which uses the request
  scheme; `X-Forwarded-*` headers are never trusted). `never` is refused when the request scheme is `https`.
- **A cookie-authenticated request must pass all of the following:**
  1. The feature flag is on, and the session is valid (not expired, idle or revoked; device not revoked).
  2. **State-changing methods** (POST, PUT, PATCH, DELETE):
     - `Origin` is present and in `PADOS_IDENTITY_ALLOWED_ORIGINS`. The default is
       `http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:8088,http://localhost:8088`.
     - `X-PADOS-CSRF` equals the derived token (constant-time).
     - `Sec-Fetch-Site`, if present, is `same-origin` or `none`.
  3. **Safe methods:**
     - If `Origin` is present it must be allowlisted.
     - If `Sec-Fetch-Site` is present it must not be `cross-site`.
- **Combining the token and the cookie:** the `X-PADOS-Control-Token` header path is unchanged.
  - If the header is present, it alone decides. An invalid header is 403 even with a valid cookie; there is no
    fallback.
  - If the header is absent, the cookie path is tried.
  - The loopback-binding assertion applies to both.
- **No CORS is added.** The Vite dev server proxies `/control`, `/api`, `/remote/v1` and **`/identity`** with
  `changeOrigin: false`, so the browser stays same-origin.

### 3.5 API (`/identity/v1`, JSON, `pados.error.v1` on errors)

| Method and path | Auth | Purpose |
| --- | --- | --- |
| `POST /pairing-codes` | control token header only | Mint a pairing code. Returns `{pairing_code, expires_at}`. |
| `POST /pair` | none (code) + allowlisted `Origin` | `{pairing_code, device_label}` → `Set-Cookie` + `{device_id, device_label, csrf_token, expires_at, idle_timeout_seconds}` |
| `GET /session` | cookie (optional) | `{enabled, authenticated, device_id?, device_label?, auth_method?: "device_pairing", expires_at?, idle_expires_at?, csrf_token?, providers: []}`. Unauthenticated is a 200 with `authenticated:false`. |
| `POST /logout` | cookie + CSRF + Origin | Revoke the current session and clear the cookie |
| `GET /devices` | cookie or control token | `{devices:[{device_id, device_label, paired_at, last_seen_at, revoked_at, current}]}`. No secrets. |
| `POST /devices/{device_id}/revoke` | cookie + CSRF + Origin, or control token | Revoke a device and its sessions |

Closed error codes:

- `IDENTITY_DISABLED` (404)
- `PAIRING_CODE_INVALID` (403), `PAIRING_CODE_EXPIRED` (403), `PAIRING_RATE_LIMITED` (429)
- `SESSION_REQUIRED` (403), `SESSION_EXPIRED` (403), `SESSION_REVOKED` (403)
- `CSRF_INVALID` (403), `ORIGIN_NOT_ALLOWED` (403)
- `DEVICE_NOT_FOUND` (404), `DEVICE_LIMIT` (409; reserved: phase 1 revokes the oldest device instead, see §3.3)
- `IDENTITY_STORE_CORRUPT` (503): added in implementation. The store failed closed (§3.3); no session is valid and
  nothing is written until the operator removes the file.
- `SESSION_SCOPE_DENIED` (403): security closure (§3.8). A valid device session called a control-plane route that is
  not on the session allowlist.
- `IDENTITY_STORE_INSECURE` (503): security closure (§3.9). The store directory or one of its files is readable or
  writable by someone other than the owner (Windows: another SID; POSIX: group or others). No session is valid and
  nothing is written.

`PAIRING_CODE_INVALID` covers both an unknown code and a wrong code, so the response does not reveal which.

### 3.6 CLI (works in Windows PowerShell 5.1)

- `pados device pair [--label TEXT] [--json]`
- `pados device list [--json]`
- `pados device revoke DEVICE_ID`

These commands use the control token file and the loopback API. They print no secret other than the pairing code
itself.

### 3.7 Implementation notes (phase 1 as built)

These record choices the sections above left open, and the one addition (`IDENTITY_STORE_CORRUPT`).

- **Code:** `src/pados/identity/` (`store.py`, `pairing.py`, `sessions.py`, `tokens.py`, `http.py`, `routes.py`,
  `cli.py`). The session path lives inside the single `control_auth` dependency in `src/pados/api/app.py`. Only
  routes that use it (`/control/*`, `/memory/*` and `/context/assemble`) can accept a session, and since §3.8 only
  the allowlisted ones do.
  The product-loop, graph-run and `/remote/v1` gates read only their own headers and are unchanged.
- **No header, no cookie:** the control routes answer exactly as before (`missing control token`). With a cookie and
  no header, refusals use the closed codes. An unknown or malformed cookie is `SESSION_REQUIRED`.
- **Pairing-code input:** every non-alphanumeric ASCII character is dropped, case is folded and the Crockford aliases
  I/L→1 and O→0 apply before hashing, so `ABCDE-FGHJK`, `abcde fghjk` and `abcdefghjk` are the same code.
- **Failed attempts:** they only feed the global 60 s failure window; outstanding codes are never burned. Minting a
  fourth outstanding code drops the oldest. An expired code is kept for one
  hour so that its holder hears `PAIRING_CODE_EXPIRED`; it still counts as a failed attempt.
- **`pados device pair --label`:** a default label carried by the code, used when `/pair` sends no `device_label`.
- **One server process:** the store's lock is per process, so `pados serve` must run a single worker (it does).
  Several workers could lose updates or double-spend a code; a database-backed store is the multi-process option.
- **Logout of a dead session:** refused with its code (for example `SESSION_EXPIRED`) and the cookie is cleared.
- **Cookie:** `Max-Age` equals the absolute TTL, so a reload after a browser restart still restores the session.
  `never` over HTTPS is refused by using the `__Host-` Secure variant anyway. With the flag on, `pados serve` runs
  uvicorn with `proxy_headers=False`, so `X-Forwarded-Proto` can never change the request scheme.
- **Responses:** every `/identity/v1` response is `Cache-Control: no-store`. `POST /logout` returns
  `{status:"ok", authenticated:false}` and clears the cookie. `POST /devices/{id}/revoke` returns
  `{device_id, revoked_at}`, is idempotent, and clears the cookie when the caller revoked its own device. Both accept
  an empty JSON body (`{}`). An `Origin` or `Sec-Fetch-Site` violation on `GET /session` is `ORIGIN_NOT_ALLOWED`.
- **Store document:** `<store>/sessions.json`, schema `pados.identity.store.v1`, holds devices, session hashes,
  pairing-code hashes and the recent failure times. Ended sessions are pruned 7 days after they end, and revoked
  devices 30 days after revocation. A symbolic link in place of the document or the key is treated as corruption.
- **Windows:** `os.chmod` cannot express owner-only. Superseded by §3.9: the store directory now gets an explicit,
  protected DACL when it is created, and every load checks it.
- **CLI token file:** `--token-file`, else `<PADOS_IDENTITY_STORE_DIR>/../control.token` (the `pados init` layout),
  else `<PADOS_HOME>/config/control.token` (installer layout). `--base-url` must be a loopback address. Refusals
  print one closed code on standard error and exit 1.

### 3.8 Least privilege for device sessions (security closure A2)

A device session no longer carries the control token's full authority. `src/pados/identity/scopes.py` holds one
**allowlist** of `(method, route template)` pairs a session may call (`SESSION_ALLOWED`). Anything else is refused
with `SESSION_SCOPE_DENIED` (403), so the rule fails closed.

- **Where it is enforced:** inside the single `control_auth` dependency (`src/pados/api/app.py`), on the cookie path
  only, after the session is authenticated (so a dead cookie still gets its own code, such as `SESSION_EXPIRED`).
- **What is compared:** the template FastAPI matched (`request.scope["route"].path`), never the raw URL. A request
  without a matched route is refused. `HEAD` is treated as `GET`.
- **The control token header** is unchanged byte for byte and keeps full authority on every route.
- **Logs:** a denial logs `identity.session.scope_denied` with the method and the route template only.
- **Classification test:** `SESSION_DENIED` records the routes kept from sessions on purpose, each with its reason.
  It grants nothing; it exists so that `tests/identity/test_session_scopes.py` can enumerate every route that
  depends on `control_auth` and fail when one is neither allowed nor denied, when a denial names a route that is not
  there, or when an allowlist entry names a route that is not mounted.
- **Extending it:** add the pair to `SESSION_ALLOWED` in the same change as the route, with a row below and a test
  case. A marked placeholder in `scopes.py` reserves the planned `POST /control/projects` and project-document
  routes from another branch; they are **not** listed, because this branch does not mount them.
- **Not affected:** `/identity/v1/*` keeps its own rules (§3.5): a session may list and revoke devices but never mint
  pairing codes. The actor surface (§4.2), `/remote/v1`, the product-loop and graph-run gates read only their own
  headers or cookies.

Route classification (kept in step with `scopes.py`; 49 `control_auth` routes after the product work loop was integrated, plus the public `/control/health`):

| Method | Route template | Control token | Device session | Rationale |
| --- | --- | --- | --- | --- |
| POST | `/context/assemble` | allowed | **denied** | Assembles a context packet from the memory store across scopes; an operator (control token) action, not a browser one |
| GET | `/control/approvals` | allowed | allowed | Control Center read |
| POST | `/control/approvals` | allowed | allowed | Control Center action (CSRF + Origin) |
| GET | `/control/approvals/{approval_id}` | allowed | allowed | Control Center read |
| POST | `/control/approvals/{approval_id}/approve` | allowed | allowed | Approval decision; a ResultCandidate approval still needs WebAuthn step-up (`RESULT_ACCEPTANCE_REQUIRES_STEP_UP`) |
| POST | `/control/approvals/{approval_id}/reject` | allowed | allowed | Approval decision; a ResultCandidate approval still needs WebAuthn step-up (`RESULT_ACCEPTANCE_REQUIRES_STEP_UP`) |
| POST | `/control/approvals/{approval_id}/revise` | allowed | allowed | Approval decision; a ResultCandidate approval still needs WebAuthn step-up (`RESULT_ACCEPTANCE_REQUIRES_STEP_UP`) |
| POST | `/control/approvals/{approval_id}/revoke` | allowed | allowed | Control Center action (CSRF + Origin) |
| GET | `/control/health` | public | allowed | Public health probe (no `control_auth`); listed for the UI contract |
| GET | `/control/jobs` | allowed | allowed | Control Center read |
| GET | `/control/jobs/{job_id}` | allowed | allowed | Control Center read |
| GET | `/control/jobs/{job_id}/artifacts` | allowed | allowed | Control Center read |
| POST | `/control/jobs/{job_id}/cancel` | allowed | allowed | Control Center action (CSRF + Origin) |
| GET | `/control/loops` | allowed | allowed | Control Center read |
| POST | `/control/loops` | allowed | allowed | Control Center action (CSRF + Origin) |
| GET | `/control/loops/{loop_id}` | allowed | allowed | Control Center read |
| POST | `/control/loops/{loop_id}/cancel` | allowed | allowed | Control Center action (CSRF + Origin) |
| GET | `/control/loops/{loop_id}/events` | allowed | allowed | Control Center read |
| GET | `/control/loops/{loop_id}/iterations` | allowed | allowed | Control Center read |
| POST | `/control/loops/{loop_id}/pause` | allowed | allowed | Control Center action (CSRF + Origin) |
| POST | `/control/loops/{loop_id}/result-candidate` | allowed | allowed | Submits a candidate for review; acceptance stays with step-up |
| POST | `/control/loops/{loop_id}/resume` | allowed | allowed | Control Center action (CSRF + Origin) |
| POST | `/control/loops/{loop_id}/start` | allowed | allowed | Control Center action (CSRF + Origin) |
| POST | `/control/loops/{loop_id}/step` | allowed | allowed | Control Center action (CSRF + Origin) |
| POST | `/control/projections/render` | allowed | **denied** | Writes projection files into the vault on disk |
| GET | `/control/projects` | allowed | allowed | Control Center read |
| POST | `/control/projects` | allowed | allowed | Product work loop: creates a project folder under the approved workspace root (no path or id typed); CSRF + Origin |
| GET | `/control/projects/{project_id}/documents` | allowed | allowed | Product work loop read (Markdown list with size and SHA-256) |
| POST | `/control/projects/{project_id}/documents` | allowed | allowed | Product work loop: `.md` only, ≤ 256 KiB, inside the project root, no silent overwrite, audited; CSRF + Origin |
| GET | `/control/projects/{project_id}/documents/content` | allowed | allowed | Product work loop read (one Markdown document) |
| GET | `/control/projects/{project_id}/work` | allowed | allowed | Product work loop read (projection only) |
| GET | `/control/jobs/{job_id}/artifacts/{artifact_type}/content` | allowed | allowed | Evidence read: bounded text, only while its SHA-256 matches the receipt |
| GET | `/control/projects/{project_id}` | allowed | allowed | Control Center read |
| GET | `/control/projects/{project_id}/activity` | allowed | allowed | Control Center read |
| GET | `/control/projects/{project_id}/context-summary` | allowed | allowed | Control Center read |
| GET | `/control/projects/{project_id}/messages` | allowed | allowed | Control Center read |
| POST | `/control/projects/{project_id}/messages` | allowed | allowed | Control Center action (CSRF + Origin) |
| GET | `/control/sync/events` | allowed | allowed | Control Center read |
| POST | `/control/task-goals` | allowed | allowed | Control Center action (CSRF + Origin) |
| GET | `/control/task-goals/{goal_id}` | allowed | allowed | Control Center read |
| POST | `/control/task-plans` | allowed | allowed | Control Center action (CSRF + Origin) |
| GET | `/control/task-plans/{plan_id}` | allowed | allowed | Control Center read |
| POST | `/control/work-requests` | allowed | allowed | Control Center action (CSRF + Origin) |
| GET | `/control/work-requests/{request_id}` | allowed | allowed | Control Center read |
| POST | `/control/work-requests/{request_id}/route` | allowed | allowed | Control Center action (CSRF + Origin) |
| GET | `/control/workers` | allowed | allowed | Control Center read |
| GET | `/control/workers/{worker_id}/health` | allowed | allowed | Control Center read |
| POST | `/memory/facts` | allowed | **denied** | Memory write: adds a fact to long-term memory |
| POST | `/memory/facts/{memory_id}/promote` | allowed | **denied** | Memory write: promotes a fact's trust level |
| GET | `/memory/search` | allowed | allowed | Read-only memory search for the project views |

### 3.9 File-system protection of the store and transport limits (security closure A3)

**Windows: protected from creation.** `src/pados/identity/fs_security.py` creates the store directory with
`CreateDirectoryW` and a `SECURITY_ATTRIBUTES` descriptor built by
`ConvertStringSecurityDescriptorToSecurityDescriptorW` from

```
O:<user SID>D:P(A;OICI;FA;;;<user SID>)(A;OICI;FA;;;SY)
```

- `<user SID>` comes from this process's access token (`OpenProcessToken`, `GetTokenInformation(TokenUser)`,
  `ConvertSidToStringSidW`). It never comes from `USERNAME` or any other environment value.
- `P` (protected) blocks inheritance from the parent. `OICI` makes both ACEs inherit to every file created inside
  (`sessions.json`, `server.key` and their `mkstemp` staging files), so a secret file is protected from its first
  byte. `os.replace` and `os.link` keep that descriptor.
- `Administrators` is **not** granted at creation.

**Windows: checked on every load.** Every `load`, `save` and server-key read calls `GetNamedSecurityInfoW` on the
directory and on each existing store file. It fails closed with `IDENTITY_STORE_INSECURE` when:

- the owner is not this user, `SYSTEM` or `Administrators`;
- the DACL is NULL (which grants everyone everything);
- an allow ACE grants another SID anything beyond synchronize, read-control, read-attributes or traverse. Read and
  write data, extended attributes, attributes, delete, `WRITE_DAC`, `WRITE_OWNER`, generic rights and
  `MAXIMUM_ALLOWED` all count, and inherit-only ACEs count too, because new files would receive them;
- an ACE type is not understood (object or compound ACEs). Deny ACEs only restrict and are ignored.

**Why `Administrators` is accepted on load.** It appears on every Windows user profile's inherited ACL, so a store
created before this check or by an elevated shell carries it. Its members already control the machine (the backup
and take-ownership privileges bypass any DACL). The installer's owner-only contract (`packages/cli/lib/home.js`,
`classifyWindowsAcl`) accepts it on the same grounds. `CREATOR OWNER` and `OWNER RIGHTS` are accepted because they
resolve to the (checked) owner.

**A directory that already exists** is checked, never re-permissioned. If it is open to others the store refuses
before writing anything; the operator removes it, and PADOS recreates it protected. The reason word is logged
(`identity.store.insecure`), never a path, a SID or an ACL dump.

**POSIX.** The directory is created by `os.mkdir(path, 0o700)` (then made exactly `0700`), and files are `0600` from
`mkstemp`. Every load requires the effective uid as owner and no group or other bits.

**What this does not claim.** It protects the store's files. It does not authenticate a Windows user: binding a
session to a Windows account still needs the OS-attested channel of §5.

**Transport limits.**

- **Non-loopback hosts.** With `PADOS_IDENTITY_SESSIONS_ENABLED=true` and a non-loopback `PADOS_API_HOST`, sessions
  refuse (`IDENTITY_SECURE_COOKIE_REQUIRED`) unless `PADOS_IDENTITY_COOKIE_SECURE=always`, so only the `__Host-`
  Secure cookie could ever be issued. With `always`, the control plane's loopback-binding rule still refuses
  (`control api must bind to localhost`).
- Both `pados serve` and every path that issues or honours a session (pairing, the session cookie on control and identity routes, actor routes, OIDC start and callback) apply both checks, so
  every combination fails closed. v1 has no supported non-loopback session deployment.
- **Cross-port localhost.** Browsers scope cookies by host, not port, and every `http://127.0.0.1:<port>` page is
  *same-site* with the Control Center, so `SameSite=Strict` does not stop another local web app from sending the
  cookie. The defences:
  - A state-changing request needs an allowlisted `Origin`, the CSRF header (which that page cannot read) and
    `Sec-Fetch-Site` of `same-origin` or `none`.
  - A read carrying a foreign `Origin` is refused.
  - No CORS header is ever sent, so a no-cors read is opaque to the page that made it.
  - **Residual:** such a page can trigger side-effect-free session reads and learn only their timing.
- Tests: `tests/identity/test_store_protection.py` and `tests/identity/test_transport_and_cross_port.py`.

### 3.10 Single sign-on in the Control Center (security closure A4)

- **Where:** a **單一登入 (SSO)** section on the **This browser & devices** page (`web/src/sso.ts`,
  `web/src/sso-panel.tsx`). It lists the providers from `GET /identity/v1/session`, keeping only a `start_url` equal
  to `/identity/v1/oidc/<id>/start` (same-origin, exact). It is hidden when there is no provider and no actor
  session. The pairing panel only points to it.
- **Return from the provider:** `?sso=ok` or `?sso_error=<CODE>` is read once per page load. The message is zh-TW
  first, with the closed code in an expandable 技術細節 detail; a malformed code is shown as
  `SSO_ERROR_UNRECOGNIZED`. `history.replaceState` then removes both parameters and keeps every other parameter and
  the fragment. Such a return opens the Control Center on that page. Set `post_login_redirect` to `/operator.html`
  so the browser lands on the operator console (`/` is the Remote console, which ignores the parameters).
- **Signed in:** with an actor session the section shows, from `GET /identity/v1/me`, the display name, actor id,
  provider, issuer **host**, expiry and a per-project roles table. It states that the roles grant no control-token
  authority and no result acceptance. An actor the server no longer binds is shown as signed out.
- **Logout:** `POST /identity/v1/actor/logout` (new, §4.2) ends only the actor session. `POST /logout` would end the
  device session whenever a paired device cookie travels too.
- **Browser storage:** the actor CSRF value stays in React state. Nothing is written to `localStorage`,
  `sessionStorage`, IndexedDB or `document.cookie`, which the Vitest suite checks.

## 4. Phase 2: OIDC (Microsoft Entra ID, Google) and PADOS actors

- **Provider adapter interface.** One generic OIDC implementation with presets for Entra
  (`https://login.microsoftonline.com/{tenant}/v2.0`) and Google (`https://accounts.google.com`).
  - Flow: Authorization Code with **PKCE S256**, plus `state` (single use, 10 min) and `nonce`.
  - ID-token validation: `iss`, `aud`, `exp`, `iat`/`nbf` (with skew), `nonce`, `azp` when there are several
    audiences, and the **RS256 signature against the provider JWKS**. The signature check is pure Python, like the
    existing WebAuthn verifier, so no new dependency is needed. The JWKS cache has a TTL; an unknown `kid` triggers
    one refetch.
  - Discovery comes from `/.well-known/openid-configuration` over HTTPS only.
- **Identity key = (issuer, subject).** Email is display data and never an identifier.
- **Configuration:** a file `PADOS_IDENTITY_OIDC_CONFIG` holding the client id and a client secret **reference**
  (an environment variable name), never a secret value. With no configuration, providers are hidden in
  `GET /session` and their endpoints refuse with `OIDC_PROVIDER_NOT_CONFIGURED`.
- **Testing without a tenant:** unit and E2E tests use a local fake issuer (generated RSA key, fake JWKS and token
  endpoint over a mocked transport). Real Entra and Google are **NOT_LIVE_VALIDATED**.
- **Actors and project roles.**
  - An `ActorBinding` maps `(issuer, subject)` to a PADOS `actor_id`.
  - `ProjectRole` grants per project: `viewer`, `contributor`, `approver`.
  - A successful SSO login **without a binding grants nothing** (`SSO_IDENTITY_NOT_BOUND`).
  - A binding grants only the explicitly configured roles. No role confers control-token authority, administration
    or result acceptance.
  - Result acceptance always requires the existing remote actor's WebAuthn step-up.

### 4.1 Live validation status: NOT_LIVE_VALIDATED

**Microsoft Entra ID and Google are NOT_LIVE_VALIDATED.** Phase 2 has been exercised only against a local fake
issuer: TEST ONLY RSA keys generated in-process, and fake discovery, JWKS and token endpoints behind an
`httpx.MockTransport`. No real tenant, app registration, consent screen or provider-issued token has been used.
Presets and claim rules follow the providers' published documentation. Until an owner signs in once against each
real provider and records the result, treat SSO as a configured-but-unproven path. Setup steps are in
[OIDC_SETUP.md](OIDC_SETUP.md).

### 4.2 Implementation notes (phase 2 as built)

**Code.**

| Module | Content |
| --- | --- |
| `src/pados/identity/oidc/config.py` | Configuration, the three presets, refusal of plaintext credentials |
| `src/pados/identity/oidc/jws.py` | Compact JWS parsing, JWK selection, pure-Python RS256 verification |
| `src/pados/identity/oidc/provider.py` | Discovery, the JWKS cache, the code exchange |
| `src/pados/identity/oidc/idtoken.py` | Claim checks |
| `src/pados/identity/oidc/flow.py` | Start and callback, the state store |
| `src/pados/identity/oidc/routes.py` | The two OIDC routes |
| `src/pados/identity/actors.py` | Actor bindings and roles |
| `src/pados/identity/actor_sessions.py` | Actor sessions |
| `src/pados/identity/actor_http.py` | Actor cookies and request rules |
| `src/pados/identity/actor_routes.py` | `/me` and actor work requests |
| `src/pados/identity/plaintext.py` | The plaintext-credential refusal shared by both configuration files |

No new dependency, no database migration. Unchanged: the scheduler, router, CAS, ScopeVersion, ResultCandidate,
`/remote/v1` authentication, step-up, and `control_auth`.

**Configuration** (both files are owner-written JSON; both unset by default):

- `PADOS_IDENTITY_OIDC_CONFIG`, schema `pados.identity.oidc.v1`:
  - Top level: `{schema, post_login_redirect, providers[1..8]}`.
  - A provider is `{id, kind, display_name?, issuer?, tenant?, allowed_tenant_ids?, client_id, client_secret_env,
    redirect_uri, scopes?}`.
  - `kind` is `entra`, `google` or `generic`. `scopes` defaults to `openid profile email` and must include `openid`.
  - Unknown fields are refused.
- `PADOS_IDENTITY_ACTORS_CONFIG`, schema `pados.identity.actors.v1`:
  - Top level: `{schema, actors[]}`.
  - An actor is `{actor_id, display_name?, issuer, subject, project_roles: {project_id: [role...]}}`.
  - Roles are the closed set `viewer`, `contributor`, `approver`.
  - An `actor_id` is unique, and an `(issuer, subject)` pair is bound to at most one actor.
- **Credential references only.** `client_secret_env` names an environment variable, which must match
  `PADOS_OIDC_[A-Z0-9_]+` so the configuration cannot point at an unrelated server credential. The value is read at
  code-exchange time.
  - A file holding a credential-shaped field (`client_secret`, `*_token`, `password`, `private_key`,
    `client_assertion` ...) at any depth is refused whole with `IDENTITY_PLAINTEXT_CREDENTIAL_FORBIDDEN`. This
    follows `src/pados/remote/auth.py`.
  - Project ids under `project_roles` are data keys and are not inspected as field names.
- **Presets.**
  - Entra: the tenant must be a lower-case directory GUID, because tokens carry the GUID issuer and a domain name
    could never match. `common` and `organizations` are accepted only with `allowed_tenant_ids`: the discovery
    issuer must then be the `{tenantid}` template, and a token's `iss` must equal
    `https://login.microsoftonline.com/<tid>/v2.0` for a pinned `tid`. `consumers` is refused.
  - Google: both documented `iss` forms (`https://accounts.google.com` and `accounts.google.com`) are compared
    exactly and recorded as one issuer, `https://accounts.google.com`.
  - Generic: an HTTPS issuer.
- **`redirect_uri`** must be HTTPS, or HTTP on `127.0.0.1`, `localhost` or `::1`, with exactly the path
  `/identity/v1/oidc/<id>/callback`. Userinfo, query and fragment are refused.
- **`post_login_redirect`** (default `/`) is a same-origin path or a URL on an allowlisted origin
  (`PADOS_IDENTITY_ALLOWED_ORIGINS`). It has no query or fragment.
- **A configuration that fails to load** is logged by code and reason word. Providers are then hidden, and every
  OIDC endpoint answers its closed code. The app still starts.

**Flow.**

- `GET /identity/v1/oidc/{id}/start`:
  1. Checks the flag, the loopback binding, `Origin` and `Sec-Fetch-Site` (a cross-site page cannot start a
     sign-in), and that the provider is configured.
  2. Fetches discovery over HTTPS (cached one hour). The discovery `issuer` must equal the expected one exactly,
     every endpoint must be HTTPS, and an advertised `code_challenge_methods_supported`, `response_types_supported`
     or `id_token_signing_alg_values_supported` must include `S256`, `code` and `RS256`.
  3. Creates a 256-bit `state` and a 256-bit pre-auth value. The store keeps SHA-256(state) with the provider id,
     SHA-256(pre-auth value) and a 10-minute expiry, at most 32 pending (the oldest is dropped). The PKCE verifier
     (`HMAC(server key, "oidc-pkce:" + state)`, 43 characters) and the nonce (`HMAC(server key, "oidc-nonce:" +
     state)`) are derived, never stored.
  4. Answers 302 to the authorization endpoint with `response_type=code`, the exact `redirect_uri`, the scopes,
     `state`, `nonce` and `code_challenge` (S256). It sets the pre-auth cookie: `pados_oidc_preauth`, or
     `__Host-pados_oidc_preauth` over HTTPS; HttpOnly, `SameSite=Lax` (the return is a cross-site top-level
     navigation), `Path=/`, `Max-Age=600`.
- `GET /identity/v1/oidc/{id}/callback`:
  1. A repeated query parameter counts as a missing state.
  2. The state must exist, belong to this provider and be presented with the matching pre-auth cookie (constant-time
     compare). Only then is it consumed. A state presented by another browser is refused without being consumed, and
     the browser's pre-auth cookie is left alone, so a forged callback link cannot cancel a sign-in in progress. An
     expired state is consumed and refused.
  3. A provider `error` parameter becomes `OIDC_PROVIDER_ERROR`; its text is never read out.
  4. The code is exchanged (form POST with `client_secret_post`, the derived `code_verifier` and the exact
     `redirect_uri`). Redirects are not followed, responses are bounded at 1 MiB, and the HTTP client ignores proxy
     and `.netrc` environment settings (`trust_env=False`). Access and refresh tokens are discarded unread.
  5. The ID token is validated:
     - Parse: at most 16 KiB, three non-empty unpadded base64url parts, JSON-object header and payload, `alg`
       exactly `RS256`. `none`, `HS*`, `ES*` and `PS*` are refused before any key is read. `kid` is required. A
       header carrying `jwk`, `jku`, `x5u`, `x5c` or `crit` is refused.
     - Key: from the JWKS cache (one hour). An unknown `kid` triggers exactly one refetch, then a refusal. A JWK is
       usable only with `kty: RSA`, a `use` of `sig` (if present), an `alg` of `RS256` (if present), a minimally
       encoded modulus of 2048 to 8192 bits and an odd exponent. Duplicate `kid`s are dropped.
     - Signature: `pow(s, e, n)`, then a constant-time comparison with the complete EMSA-PKCS1-v1_5 encoding (SHA-256
       `DigestInfo` DER prefix included). The signature length must equal the modulus length, and `0 < s < n`.
     - Claims: `iss` exact; `aud` contains the client id; `azp` required when there are several audiences and equal
       to the client id whenever present; `exp`, `nbf` and `iat` within at most 120 s of skew; `nonce` exact; `sub`
       present (at most 255 printable characters); for Entra, `tid` consistent with the pinned tenant.
  6. The identity key `(issuer, subject)` is looked up in the actor directory. Email is never compared. If there is
     no binding, no session is issued (`SSO_IDENTITY_NOT_BOUND`); the server log records the issuer and subject at
     info level so the owner can bind it.
  7. On success, an actor session is issued and the response is 302 to `post_login_redirect` with `?sso=ok`. On any
     refusal it is 302 with `?sso_error=<CLOSED_CODE>` only.
  8. Both OIDC responses carry `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.

**Actor sessions.**

- Built from the phase 1 machinery: a `secrets.token_urlsafe(32)` id stored only as SHA-256, the derived CSRF value,
  the same absolute and idle lifetimes, the control-token binding (rotating the control token ends actor sessions
  too) and the same store.
- They are kept apart **structurally**:
  - Records live in `actor_sessions` in the store document, never in `sessions`. `IdentityService.resolve`, the only
    lookup `control_auth` performs, cannot find them.
  - The cookie is `pados_actor` (`__Host-pados_actor` over HTTPS), which `control_auth` never reads.
- A record carries `auth_method: "oidc"`, `actor_id`, `provider_id`, `issuer` and `subject`.
- Limits: 4 live sessions per actor and 64 in total; beyond that the oldest is revoked. A new sign-in revokes the
  actor session the browser presented (fixation).
- Every actor request re-checks that the identity is still bound to the same actor and that its provider is still
  configured. Removing a binding or a provider ends the session's authority immediately.
- **Store document:** `actor_sessions` and `oidc_states` are additive optional maps, keyed by SHA-256 hex. They are
  omitted while empty, so a phase 1 document stays byte-identical and phase 1 rollback (delete `sessions.json`) is
  unchanged.

**API additions** (closed codes, `pados.error.v1`):

| Method and path | Auth | Purpose |
| --- | --- | --- |
| `GET /oidc/{provider_id}/start` | none (flag, loopback, Origin) | 302 to the provider; sets the pre-auth cookie |
| `GET /oidc/{provider_id}/callback` | state + pre-auth cookie | 302 to the Control Center with `?sso=ok` or `?sso_error=CODE` |
| `GET /me` | actor cookie (bound) | `{actor_id, display_name, auth_method:"oidc", provider_id, issuer, subject, projects:[{project_id, roles}], expires_at, idle_expires_at, csrf_token}` |
| `POST /projects/{project_id}/work-requests` | actor cookie + CSRF + Origin, `contributor` on the project | `{title, body, idempotency_key (≤48), requested_capability?}` → the control route's response |
| `POST /actor/logout` | actor cookie + CSRF + Origin (no binding check) | Ends only the presented actor session and clears its cookie; a device session is untouched (security closure A4, §3.10) |

- **`GET /session`** keeps its phase 1 meaning: `authenticated` is the device session, the only session the control
  plane accepts. It now lists `providers` (`[{id, kind, display_name, start_url}]`, empty unless both configuration
  files loaded) and, when an actor session is presented, `actor: {auth_method:"oidc", actor_id, provider_id, issuer,
  subject, expires_at, idle_expires_at}`. `actor` is absent otherwise.
- **`POST /logout`** without a device cookie ends the presented actor session (cookie, CSRF and Origin required).
- **Work-request creation** delegates to the single creation function that `POST /control/work-requests` also
  calls (`create_work_request_entry` in `src/pados/api/app.py`). The body forbids `project_id` and `actor_id`. The
  idempotency key is namespaced as `actor:<actor_id>:<key>`.
- **What each role grants:**
  - `contributor`: creating a work request on its project, and nothing else.
  - `viewer` and `approver`: reported by `/me`; they grant no action in phase 2.
  - No role reaches `/control/*`, `/memory/*`, `/context/assemble`, device management, pairing codes, approvals or
    `/remote/v1`. Tests enumerate every `/control/*` route.
- **New closed codes:**
  - `OIDC_PROVIDER_NOT_CONFIGURED` (404)
  - `OIDC_CONFIG_INVALID` (503), `ACTORS_CONFIG_INVALID` (503), `IDENTITY_PLAINTEXT_CREDENTIAL_FORBIDDEN` (503)
  - `OIDC_STATE_INVALID` (403), `OIDC_TOKEN_INVALID` (403)
  - `OIDC_PROVIDER_ERROR` (502), `OIDC_PROVIDER_UNAVAILABLE` (503), `OIDC_CLIENT_CREDENTIAL_UNAVAILABLE` (503)
  - `SSO_IDENTITY_NOT_BOUND` (403), `ACTOR_ROLE_REQUIRED` (403)

**Logs.** OIDC events carry provider ids, actor ids, closed codes and fixed reason words. They never carry a code,
state, nonce, verifier, ID token, client credential, session id or CSRF value.

- With the identity flag on, `pados serve` also runs uvicorn with `access_log=False`, because uvicorn's access log
  prints raw query strings (the callback carries the code and state). The app's own `http.request.finished` log
  records the route template only.

**Web.** Built in the security closure: see §3.10 (sign-in section, return messages, actor view, actor logout).

## 5. Windows Hello and trusted Windows SID (plan only, phase 3)

- Windows Hello in the browser is already a WebAuthn platform authenticator. It can serve as the step-up device
  through the existing presence enrollment, with no new trust.
- Binding a session to a **Windows account SID** needs an OS-attested channel, never a self-reported username, an
  HTTP header or browser JS. The plan:
  1. A loopback-only named-pipe companion exposed by `pados serve`.
  2. The server calls `ImpersonateNamedPipeClient` / `GetNamedPipeClientProcessId` and reads the client token's user
     SID.
  3. It compares that SID with an allowlist written at `pados init` (the installer's SID).
  4. The CLI pairing request is then attested by SID, in addition to the control token.
- The browser never asserts a SID. This is not implemented in phase 1/2 and is recorded as NOT_IMPLEMENTED.

## 6. Feature flags and rollback

- `PADOS_IDENTITY_SESSIONS_ENABLED=false` (default). Off means: `/identity/v1` answers `IDENTITY_DISABLED` (except
  `GET /session`, which reports `enabled:false`); cookies are ignored; the token-in-memory UI is unchanged.
- `PADOS_IDENTITY_OIDC_CONFIG` unset (default) means no SSO: `providers` is `[]` and every OIDC endpoint answers
  `OIDC_PROVIDER_NOT_CONFIGURED`. `PADOS_IDENTITY_ACTORS_CONFIG` unset binds nobody, so every SSO sign-in is
  refused with `SSO_IDENTITY_NOT_BOUND`. Both require `PADOS_IDENTITY_SESSIONS_ENABLED=true`.
- **Rollback of phase 2:** unset `PADOS_IDENTITY_OIDC_CONFIG`, which removes the providers and ends the authority of
  every actor session at its next request, or remove an actor's binding to end that actor's authority. No store or
  database change is needed.
- **Rollback:** set the flag to `false` (sessions stop working immediately), or delete `<store>/sessions.json` (all
  sessions revoked). No database migration is involved.
