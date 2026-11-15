# Setting up SSO (Microsoft Entra ID, Google) for PADOS

Status: **NOT_LIVE_VALIDATED.** These steps follow the providers' published documentation. They have not been
carried out against a real Entra ID tenant or a real Google Cloud project. The implementation has been tested only
against a local fake issuer (see [UNIFIED_IDENTITY_SESSION_DESIGN.md](UNIFIED_IDENTITY_SESSION_DESIGN.md) §4.1).
Record the outcome of your first real sign-in for each provider before you rely on it.

SSO in PADOS proves *who* someone is. It grants nothing by itself:

- The identity must be bound to a PADOS actor, which then has explicit per-project roles.
- An SSO session never passes the control plane. Pairing a browser with a code, or the control token, is still the
  way to operate the Control Center.
- Result acceptance still needs the remote actor's WebAuthn step-up.

## 1. Choose the redirect URI

The redirect URI must be exact. It is the same string at the provider and in the PADOS configuration:

```text
http://127.0.0.1:8088/identity/v1/oidc/<provider id>/callback
```

- `<provider id>` is the `id` you give the provider in the PADOS configuration (for example `entra` or `google`).
- PADOS accepts HTTPS, or HTTP only on `127.0.0.1`, `localhost` or `::1`. The path must be exactly
  `/identity/v1/oidc/<provider id>/callback`, with no query or fragment.
- If the Control Center runs through the Vite dev server, use its origin instead (for example
  `http://127.0.0.1:5173/identity/v1/oidc/entra/callback`). Vite proxies `/identity` unchanged.
- Sign in from the same host name that the redirect URI uses (`127.0.0.1` and `localhost` are different sites for
  cookies).

Scopes: `openid profile email`. Only `openid` is required; email is display data and is never used to identify
anyone.

## 2. Register the app in Microsoft Entra ID

1. Open the Microsoft Entra admin center and go to **App registrations > New registration**.
2. Supported account types: choose **Accounts in this organizational directory only** (single tenant). This is the
   recommended setup.
3. Redirect URI: platform **Web**, value as in section 1. Entra accepts `http://localhost` and `http://127.0.0.1`
   redirect URIs for local development.
4. After you create the app, note the **Application (client) ID** and the **Directory (tenant) ID**. The tenant ID is
   a GUID; PADOS requires the GUID, not a domain name.
5. Under **Certificates & secrets**, create a client credential value and copy it. Do **not** put it in any PADOS
   file. Set it as an environment variable whose name starts with `PADOS_OIDC_` in the environment of `pados serve`,
   for example `PADOS_OIDC_ENTRA_CLIENT_SECRET`.
6. Under **Token configuration** you may add the optional `email` claim. PADOS does not need it.
7. Multi-tenant (`common` or `organizations`): PADOS refuses this unless you list every accepted directory GUID in
   `allowed_tenant_ids`. Personal Microsoft accounts (`consumers`) are not supported.

PADOS uses the `sub` claim. In Entra it is pairwise: it is specific to this app registration. If you register a new
app, every actor binding must be redone.

## 3. Register the app in Google

1. In the Google Cloud console, configure the **OAuth consent screen** (Internal for a Workspace organization, or
   External with test users).
2. Go to **APIs & Services > Credentials > Create credentials > OAuth client ID**, and choose application type
   **Web application**.
3. **Authorized redirect URIs:** add the URI from section 1.
4. Note the **Client ID**. Put the client credential value in an environment variable such as
   `PADOS_OIDC_GOOGLE_CLIENT_SECRET`, never in a file.

## 4. Write the PADOS OIDC configuration

Save a JSON file somewhere only the owner can read, for example under `runtime/local/identity/`. Point
`PADOS_IDENTITY_OIDC_CONFIG` at it. It holds no credential: a field named like one (`client_secret`, `password`,
`*_token` ...) makes PADOS refuse the whole file.

```json
{
  "schema": "pados.identity.oidc.v1",
  "post_login_redirect": "/",
  "providers": [
    {
      "id": "entra",
      "kind": "entra",
      "tenant": "<directory tenant GUID, lower case>",
      "client_id": "<application (client) ID>",
      "client_secret_env": "PADOS_OIDC_ENTRA_CLIENT_SECRET",
      "redirect_uri": "http://127.0.0.1:8088/identity/v1/oidc/entra/callback",
      "scopes": ["openid", "profile", "email"]
    },
    {
      "id": "google",
      "kind": "google",
      "client_id": "<OAuth client ID>",
      "client_secret_env": "PADOS_OIDC_GOOGLE_CLIENT_SECRET",
      "redirect_uri": "http://127.0.0.1:8088/identity/v1/oidc/google/callback"
    }
  ]
}
```

- `post_login_redirect` is where the browser lands after the callback, with `?sso=ok` or `?sso_error=<CODE>`
  appended. It is a path on the API origin, or a full URL on an origin listed in `PADOS_IDENTITY_ALLOWED_ORIGINS`
  (for example `http://127.0.0.1:5173/` for the Vite dev server).
- `kind: "generic"` takes an `issuer` (HTTPS) instead of a tenant, for any other OpenID Connect provider that signs
  ID tokens with RS256.

## 5. Bind actors and grant project roles

An SSO identity is the pair `(issuer, subject)`. To find a person's pair, ask them to sign in once. The refusal
`SSO_IDENTITY_NOT_BOUND` is logged with `issuer` and `subject` (event `identity.oidc.identity_not_bound`).

Then write the actor directory and point `PADOS_IDENTITY_ACTORS_CONFIG` at it:

```json
{
  "schema": "pados.identity.actors.v1",
  "actors": [
    {
      "actor_id": "alice",
      "display_name": "Alice",
      "issuer": "https://login.microsoftonline.com/<tenant GUID>/v2.0",
      "subject": "<sub claim from the log>",
      "project_roles": {"<project id>": ["viewer", "contributor"]}
    },
    {
      "actor_id": "bob",
      "issuer": "https://accounts.google.com",
      "subject": "<sub claim from the log>",
      "project_roles": {"<project id>": ["viewer"]}
    }
  ]
}
```

Roles form a closed set:

| Role | Grants in this phase |
| --- | --- |
| `viewer` | Nothing beyond appearing in `GET /identity/v1/me` |
| `contributor` | `POST /identity/v1/projects/<project id>/work-requests` on that project |
| `approver` | Nothing beyond appearing in `GET /identity/v1/me`. Approvals and result acceptance stay on their existing paths, and acceptance needs WebAuthn step-up. |

Removing a binding, or a provider, ends that authority at the next request.

## 6. Start and check

```text
PADOS_IDENTITY_SESSIONS_ENABLED=true
PADOS_IDENTITY_OIDC_CONFIG=<path to the OIDC file>
PADOS_IDENTITY_ACTORS_CONFIG=<path to the actor file>
PADOS_OIDC_ENTRA_CLIENT_SECRET=<set in the service environment, not in .env files you share>
```

1. Run `pados serve`. `GET /identity/v1/session` lists the providers.
2. Open `http://127.0.0.1:8088/identity/v1/oidc/entra/start` in the browser. The Control Center does not have a
   sign-in button yet.
3. After you sign in, the browser lands on `post_login_redirect` with `?sso=ok`.
4. `GET /identity/v1/me` shows the actor and its roles.

If anything is refused, the browser lands with `?sso_error=<CODE>` and the server log carries the same code with a
fixed reason word:

| Code | Usual cause |
| --- | --- |
| `OIDC_PROVIDER_NOT_CONFIGURED` | No configuration, or an unknown provider id |
| `OIDC_CONFIG_INVALID` / `IDENTITY_PLAINTEXT_CREDENTIAL_FORBIDDEN` | The configuration file was refused (see the log reason) |
| `OIDC_STATE_INVALID` | The sign-in took more than 10 minutes, was finished in another browser, or was replayed |
| `OIDC_PROVIDER_ERROR` | The provider refused (consent denied, wrong redirect URI or client credential) |
| `OIDC_PROVIDER_UNAVAILABLE` | Discovery or the JWKS could not be fetched, or the discovery issuer did not match |
| `OIDC_CLIENT_CREDENTIAL_UNAVAILABLE` | The `client_secret_env` variable is not set for `pados serve` |
| `OIDC_TOKEN_INVALID` | The ID token failed a check (issuer, audience, time, nonce, signature, tenant) |
| `SSO_IDENTITY_NOT_BOUND` | No actor binding for this `(issuer, subject)` |

## 7. Operational limits

- **Proxies.** The OIDC HTTP client does not read proxy environment variables. A host that can reach the provider
  only through a proxy is not supported in this phase.
- **Access log.** Start the API with `pados serve`. With the identity flag on, it turns off uvicorn's access log,
  which would otherwise print the callback's raw query string (code and state). If you launch uvicorn any other way,
  pass `--no-access-log` yourself.
- **Plain HTTP on loopback.** Over HTTP the cookies cannot use the `__Host-` prefix, so they are not isolated by port.
  Another web app on the same host could plant a pre-auth cookie and push its own completed sign-in into your
  browser (login CSRF, which needs an identity that is bound). Use loopback HTTP only on a single-user host, and
  HTTPS otherwise.
- **Pending sign-ins.** At most 32 are kept and none is ever evicted, so a local process calling `/start` in a loop
  cannot cancel a sign-in in progress. New starts are limited to 10 per minute; beyond that, or with 32 pending,
  `/start` answers `OIDC_START_RATE_LIMITED` (429). Residual: such a process can delay new sign-ins (availability
  only). `/start` refuses cross-site and same-site (another localhost port) browser requests.
- **Provider outages.** After a failed discovery, `/start` answers `OIDC_PROVIDER_UNAVAILABLE` for 30 s without
  contacting the provider. Malformed provider URLs and malformed token headers fail closed with a closed code.
- **Validation order.** The actor work-request route validates the request body before the actor session, as the
  control route does; a caller without a session can learn only whether a body is well formed.
