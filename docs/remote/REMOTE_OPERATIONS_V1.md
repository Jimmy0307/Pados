# PADOS Remote Operations V1

Remote Operations lets a phone or browser drive PADOS without becoming an authority.

```
PHONE / WEB / CHAT = INTERFACE
PADOS              = AUTHORITY
MODEL              = REASONING PROVIDER
COMPUTE NODE       = EXECUTION SUBSTRATE
SANDBOX            = ISOLATION SUBSTRATE
RECEIPT + VALIDATION + CAS = ACCEPTANCE
```

## What a remote client can do

| Capability | Endpoint | Authority path |
| --- | --- | --- |
| Submit intent | `POST /remote/v1/intents` | `RemoteIntentEnvelope` -> ledger -> existing work-request capture |
| Needs-attention projection | `GET /remote/v1/attention`, `GET /remote/v1/mission-control` | product-loop durable read model (projection, not authority) |
| Jobs | `GET /remote/v1/jobs`, `GET /remote/v1/jobs/{run_id}` | read model |
| Pause / resume / cancel | `POST /remote/v1/jobs/{run_id}/{pause,resume,cancel}` | execution backend request; never changes semantic state |
| Pending approvals | `GET /remote/v1/approvals` | `control_approvals` pinned to candidate + ScopeVersion |
| Server session | `POST /remote/v1/sessions` | server-issued handle (SHA-256 stored); binding context for step-up |
| Step-up challenge | `POST /remote/v1/approvals/{ref}/step-up` | server-derived binding (actor, session, project, task, candidate + digest, action, classification, ScopeVersion) |
| Human presence | `POST /remote/v1/approvals/{ref}/step-up/{challenge_id}/verify` | WebAuthn assertion with user verification -> single-use proof |
| Approve / reject | `POST /remote/v1/approvals/{ref}/{approve,reject}` | consumed step-up proof -> `control_approvals` decision -> ResultReceipt -> `StateProofService.commit_result` (CAS) |
| Lineage graph | `GET /remote/v1/jobs/{run_id}/graph` | rebuildable Work Graph projection (never authority) |
| Device enrolment | `POST /remote/v1/presence/enrollment-options`, `/presence/enrollment-descriptor` | returns a public descriptor only; enrolment is an operator edit of the actor row |
| Upload | `POST /remote/v1/uploads`, `GET /remote/v1/uploads/{id}` | PADOS staging area (STAGED / QUARANTINED) |
| Node status | `GET /remote/v1/nodes` | compute-node visibility projection (health only) |
| Home Base summary | `GET /remote/v1/home-base` | version, network model, guard status |

There is **no** shell, SQL, filesystem browse, model proxy, raw Ollama/MLX, or
chat-completion endpoint. `tests/product/test_anti_clone_boundary.py` and
`tests/remote/test_remote_api_vertical_slice.py::test_no_shell_sql_or_model_passthrough_routes`
enforce this.

## Enabling

Remote mode is off unless `PADOS_REMOTE_CONFIG` points to a JSON file:

```json
{
  "bind_host": "100.64.0.10",
  "staging_root": "/srv/pados/remote/staging",
  "ledger_path": "/srv/pados/remote/ledger/intents.jsonl",
  "inbox_root": "/srv/pados/remote/inbox",
  "allowed_origins": ["https://jimmy0307.github.io"],
  "project_workspaces": {"<project-uuid>": "/srv/pados/workspaces/<project>"},
  "actors": [
    {"actor_id": "owner", "credential_hash": "<sha256 hex of the access proof>",
     "projects": ["<project-uuid>"], "may_approve": true, "may_control_jobs": true,
     "presence_credentials": [
       {"credential_id": "<base64url>", "algorithm": -7, "public_key_cose": "<base64url COSE key>",
        "label": "owner-phone"}]}
  ],
  "presence": {"rp_id": "pados.example.com", "origins": ["https://pados.example.com"]}
}
```

`presence` names the WebAuthn relying party (RP ID and the exact origins the browser may present).
Without it, every approval and rejection fails closed (`APPROVAL_PRESENCE_NOT_CONFIGURED`).
`presence_credentials` hold public keys only; obtain a descriptor from the PWA ("Register this
device for approvals") and add it to the actor row yourself — the endpoint never changes the
configuration. The enrolment challenge is server-held, single-use, actor-bound and expires in 180 s.
Attestation statements are not verified (`attestation_verified: false`), so the user-verification flag is
the authenticator's own claim: confirm out of band that the descriptor came from the intended device
before enrolling it.

`allowed_origins` is optional and defaults to same-origin only. For a static frontend on a separate
origin, add that exact HTTPS origin. Wildcards, non-local HTTP, URL paths, and embedded user-info are
rejected. CORS only permits the browser transport; actor authentication and project authorization
remain required for every request.

Run the overlay-facing process through the guarded launcher. Only the remote surface is mounted:

```
python -m pados.remote.serve --port 8089
```

The launcher loads `PADOS_REMOTE_CONFIG` and runs the deployment guard. It then creates the one
listening socket itself, bound to exactly `bind_host` (a literal loopback or private-overlay IP,
never a wildcard), and verifies `getsockname()`. It records a listener attestation in the process
and serves only that socket through a single-worker uvicorn server. A request that arrives on a
non-loopback local address is served only when it reaches the attested socket at the configured
address. Otherwise every `/remote/v1` route answers 503 with `REMOTE_LISTENER_NOT_ATTESTED` or
`REMOTE_LISTENER_NOT_CONFIGURED_ADDRESS`.

From now on a plain `uvicorn --factory pados.remote.app:create_remote_app` launch (any `--host`)
serves the remote routes on loopback only. Do not use it for overlay access.

The intent ledger directory (`ledger_path`'s parent) must be private to the PADOS service account.
The ledger and its `.lock` file are opened without following links, and the lock is created 0600
and must be owned by the service user. A world-writable directory without the sticky bit is
refused (`LEDGER_DIRECTORY_NOT_PROTECTED`). Each intent is claimed in the ledger before dispatch,
so it is dispatched at most once across processes. A claim left without an outcome by a crash is
reported as `INTENT_CLAIM_UNRESOLVED` and is never re-dispatched automatically. To resolve it, an
operator checks the downstream idempotency record (`remote:<intent_id>`) and then calls
`IntentLedger.release(...)`, or tells the client to use a new `intent_id`.

The main API (`pados.api.app`) also mounts `/remote/v1` when the variable is set,
but it stays bound to localhost.

## Fail-closed deployment guard

`pados.remote.deployment.check_remote_deployment` refuses to serve (every route
returns 503 with the violation list) when:

* the control token hash or DB password is still the shipped development default;
* the DB host is not loopback (the DB is never exposed remotely);
* the model endpoint is bound to a wildcard address;
* the remote bind address is public, wildcard, or a hostname rather than a literal IP.

## Authentication

Every request carries `X-PADOS-Remote-Actor` and `X-PADOS-Remote-Proof`. The proof
is compared as a SHA-256 hash in constant time, reusing `pados.control.security.token_hash`.
Actors have an explicit project allowlist, and separate `may_approve` /
`may_control_jobs` permissions. The client never supplies `actor_id` or `session_id`
in a payload; any attempt is rejected (`CLIENT_IDENTITY_FORBIDDEN`).

## Fresh approval step-up

A standing authenticated session is never enough for a consequential decision. For every
APPROVE or REJECT the client:

1. opens a server session (`POST /sessions`) and sends `X-PADOS-Remote-Session` on decision calls;
2. requests a challenge naming only the approval ref and action. The server derives every binding
   field from authority: actor, session, project, task, goal, candidate id, candidate digest,
   action, classification, expected ScopeVersion, approval ref, protocol/schema version, issue and
   expiry time (120 s), and a fresh 32-byte nonce. The WebAuthn challenge is
   `SHA-256(nonce || binding_digest)`, so the signature itself commits to the binding;
3. completes a WebAuthn assertion with **user verification required** (`UP` and `UV` flags, exact
   challenge, allowed origin, RP ID hash, registered public key, ES256 or RS256, sign-counter
   regression check: once a counter was seen it must strictly increase). One attempt per challenge; a
   failure burns it. The public assertion (client data, authenticator data, signature) is stored and its
   digest is recorded in the ResultReceipt, so the approval can be re-verified later against the enrolled
   public key. At most 16 open challenges and 32 sessions per actor;
4. receives a single-use proof reference valid for 60 s, and sends it as `step_up_proof` in the
   decision intent.

The decision path recomputes the binding from current authority and consumes the proof atomically
(`UPDATE ... WHERE status='VERIFIED' AND proof_consumed_at IS NULL AND proof_expires_at > now()`).
Replay, another actor or session, another project/task/candidate, a changed candidate digest, another
action, a different classification, an expired challenge or proof, or a stale ScopeVersion are all
refused. Non-approvers are refused before any proof is examined. Proofs, nonces and session handles
never enter the intent ledger beyond their non-secret references.

WebAuthn proves a fresh, user-verified gesture for the server-chosen challenge; it does not display
the candidate on the authenticator. The PWA shows the server-returned binding (candidate, digest,
project, classification, ScopeVersion) before invoking the authenticator.

## Remote approval

A decision binds the authenticated actor, the approval ref, the exact candidate
receipt, the manifest hash, the ScopeVersion presented, the decision, and a timestamp
(`RemoteApprovalBinding.binding_hash`). Before committing, PADOS re-reads current
state and refuses with:

* `SCOPE_VERSION_CHANGED` when the scope moved;
* `CANDIDATE_MISMATCH` when the candidate or manifest differs;
* `NOT_PENDING_HUMAN_DECISION` when the run is no longer awaiting a human;
* `ACTOR_NOT_AUTHORIZED` when the actor lacks `may_approve` for the scope;
* `COMMIT_CONFLICT` when the CAS reports anything other than `COMMITTED` (for example `STALE_CANDIDATE`).

## Job control

Product-loop runs are bounded and synchronous, so the live backend reports
`PAUSE_UNSUPPORTED` / `CANCEL_UNSUPPORTED` rather than pretending. When a backend
does support cancellation, it moves through `CANCEL_REQUESTED -> CANCEL_ACKNOWLEDGED -> CANCEL_COMPLETED`.
A client disconnect never cancels a job.

## Evidence

`runtime/artifacts/reports/remote-operations-v1/` has the gate results,
the vertical slice, the mobile UAT, the context efficiency and duplication checks,
the staging and bulk-import UATs, and core regression.
