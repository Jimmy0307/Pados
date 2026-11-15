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
   device session. A session grants **exactly** the authority of the control token on the control plane, never more.
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
- At most 5 failed attempts per code (the code is then burned).
- At most 3 outstanding codes.
- A global limit of 20 failed pair attempts per 10 minutes, after which pairing pauses with
  `PAIRING_RATE_LIMITED`.
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
  `cli.py`). The session path lives inside the single `control_auth` dependency in `src/pados/api/app.py`, so every
  route that uses it (`/control/*`, `/memory/*` and `/context/assemble`) accepts a session and nothing else does.
  The product-loop, graph-run and `/remote/v1` gates read only their own headers and are unchanged.
- **No header, no cookie:** the control routes answer exactly as before (`missing control token`). With a cookie and
  no header, refusals use the closed codes. An unknown or malformed cookie is `SESSION_REQUIRED`.
- **Pairing-code input:** every non-alphanumeric ASCII character is dropped, case is folded and the Crockford aliases
  I/L→1 and O→0 apply before hashing, so `ABCDE-FGHJK`, `abcde fghjk` and `abcdefghjk` are the same code.
- **Failed attempts:** a failed pair attempt counts against every outstanding code (a guesser guesses all of them);
  a code reaching 5 is burned. Minting a fourth outstanding code drops the oldest. An expired code is kept for one
  hour so that its holder hears `PAIRING_CODE_EXPIRED`; it still counts as a failed attempt.
- **`pados device pair --label`:** a default label carried by the code, used when `/pair` sends no `device_label`.
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
- **Windows:** `os.chmod` cannot express owner-only. The store relies on the per-user ACL of `runtime/local` there,
  as `control.token` already does. The POSIX mode test is skipped on win32 for that reason.
- **CLI token file:** `--token-file`, else `<PADOS_IDENTITY_STORE_DIR>/../control.token` (the `pados init` layout),
  else `<PADOS_HOME>/config/control.token` (installer layout). `--base-url` must be a loopback address. Refusals
  print one closed code on standard error and exit 1.

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
- `PADOS_IDENTITY_OIDC_CONFIG` unset (default) means no SSO.
- **Rollback:** set the flag to `false` (sessions stop working immediately), or delete `<store>/sessions.json` (all
  sessions revoked). No database migration is involved.
