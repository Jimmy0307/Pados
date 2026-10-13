# Simple Machine Connection V1

Package: `pados.machines` (library only). The integrator wires the `pados machine add/list/test/remove`
CLI, the settings key for the inventory path and the Mission Control projection. This package adds no API
route and exposes no shell.

Security control: **SH-M01** (`docs/security/SECURITY_CONTROLS_MATRIX.md`, threat model section 6).

> PASSWORD MAY AUTHENTICATE does **not** mean PADOS STORES PASSWORD.

## 1. User experience

```
Machine:        Mac
Host:           192.168.x.x            (a LAN / overlay IP, or a hostname you confirm)
Username:       <account>
Authentication: SSH key | ssh-agent | OS credential reference | one-time password bootstrap
```

| Step | Library call | What happens |
|---|---|---|
| 1 | `ux.scan_host_key(host, probe=...)` | Reads the host key the machine presents and shows its `SHA256:` fingerprint. |
| 2 | `ux.add_machine(name, host, username, auth, store=..., probe=..., expected_fingerprint=...)` | Validates input, re-reads the key, requires the fingerprint you confirmed out of band (or `accept_new_host_key=True`, an explicit trust-on-first-use acknowledgement), derives the identity, checks key-based login and stores the profile. |
| 3 | `ux.list_machines(store)` / `ux.describe(profile)` | Rows / a short human summary. Credential references are shown by scheme only. |
| 4 | `ux.check_machine(store, name_or_id, probe)` | Connection test of an inventoried machine (`pados machine test`). |
| 5 | `ux.remove_machine(store, name_or_id)` | Removes the profile and its pinned key. |
| - | `connection.update_location(...)` | The same machine at a new address (same host key). |
| - | `ux.acknowledge_host_key_alert(...)` / `ux.repin_machine(...)` | Human-only recovery after `HOST_KEY_MISMATCH`. |

`auth` is one of `ssh-key` (needs `auth_ref=secretref://...`), `ssh-agent` (default reference
`ssh-agent://default`), `os-credential` (`oscred://<store>/<name>`) or `password-bootstrap` (section 5).

## 2. Profile schema

JSON Schema: [`MACHINE_PROFILE_SCHEMA.json`](MACHINE_PROFILE_SCHEMA.json) (generated from the model; a test
keeps it in sync). Frozen pydantic models with `extra="forbid"`.

| Field | Meaning |
|---|---|
| `machine_id` | `mach-<20 hex>`, derived from the host key (section 3) |
| `display_name` | 1-80 printable characters, unique (case-insensitive) |
| `host`, `port`, `hostname_confirmed` | Location only. Literal private/LAN/overlay IP, or an operator-confirmed hostname |
| `username` | Login account (`[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}`, never starts with `-`) |
| `auth_method` | `SSH_KEY`, `SSH_AGENT`, `OS_CREDENTIAL_REF`, `PASSWORD_BOOTSTRAP_COMPLETED` |
| `auth_ref` | A reference, never key or password material: `secretref://...`, `ssh-agent://<name>`, `oscred://<store>/<name>` |
| `key_auth_method` | Only with `PASSWORD_BOOTSTRAP_COMPLETED`: the installed key method (`SSH_KEY` or `SSH_AGENT`) |
| `platform` | `MACOS`, `LINUX`, `WINDOWS` |
| `roles` | Any subset of `HOME_BASE`, `LOCAL_COMPUTE_NODE`, `HERDR_MACHINE`, `SANDBOX_HOST` |
| `host_public_key`, `host_key_fingerprint` | The pinned public host key and its `SHA256:<base64>` fingerprint |
| `enrolled_at`, `connection_state`, `state_observed_at`, `last_seen` | Observation (section 6) |
| `identity_alert` | `HOST_KEY_MISMATCH` while a spoof alert is open (forces `UNAUTHORIZED`) |
| `herdr` | `{enabled, ref}`: an opaque socket/ref string only; `enabled` requires the `HERDR_MACHINE` role |
| `compute_node_id` | Optional `lcn-<20 hex>` link; requires the `LOCAL_COMPUTE_NODE` role |

There is no password, passphrase or private-key field anywhere. Every raw input passes the shared
`assert_minimized` guard against the explicit field schema *before* validation (a field named like a
credential, or a credential-shaped value such as a private key header or `name=value` assignment, is refused
with a code), and every persisted record passes it again. Validation errors are re-raised as code-only
`MachineError`s, because pydantic's own error text echoes input values.

The inventory file (`pados.machines.inventory.v1`) holds `security_profile` (`TRUSTED_LAN` default, or
`LOCAL_SINGLE_USER`), `stale_after_seconds` (default 300) and the profiles. It is written atomically with
mode 0600 under a cross-process lock. A missing file is an empty inventory; a corrupt, tampered, symlinked,
oversized or group/world-readable file fails closed (`MACHINE_INVENTORY_INVALID`,
`MACHINE_INVENTORY_PERMISSIONS_TOO_OPEN`) and is never read as empty.

## 3. Identity

```
machine_id = "mach-" + sha256("pados-machine-identity\n" + <SSH host public key blob>)[:20]
```

* Hostname and IP are **location, not identity**.
* The same host key at a new address is the same machine: `update_location` moves it after the new address
  presents the pinned key and passes a strict SSH check.
* A different host key at a known address is `HOST_KEY_MISMATCH`: state `UNAUTHORIZED`, a sticky
  `identity_alert`, never silently re-enrolled. Only a human clears it, either by
  `acknowledge_host_key_alert` (re-confirming the *pinned* fingerprint while the machine presents it again)
  or by `repin_machine` (confirming a *new* fingerprint after a legitimate reinstall, which creates a new
  `machine_id`; compute nodes still pointing at the old id are then flagged).
* Two profiles for one host key, one location (host + port) or one display name are refused
  (`MACHINE_ALREADY_ENROLLED`, `MACHINE_DUPLICATE_IDENTITY`, `MACHINE_LOCATION_DUPLICATE`,
  `MACHINE_DISPLAY_NAME_DUPLICATE`), on add and on load.
* The PADOS-managed `pados_known_hosts` file next to the inventory has one line per machine keyed by
  `machine_id`; the SSH probe uses `HostKeyAlias=<machine_id>`, so the key is pinned to the identity.

## 4. Authentication methods and the credential policy

The inventory says *which* reference a profile uses; it is **not** permission to use it (CR-07 MC-03). A
credential is used only when the operator credential policy allows it: a separate owner-only file
(`machine_credential_policy.json` next to the inventory by default, `--credential-policy` on the CLI):

```json
{"schema_version": "pados.machines.credential-policy.v1",
 "machines": {"mach-<20 hex>": {"username": "<account>",
                                "allowed_auth_refs": ["secretref://env/PADOS_MAC_KEY", "ssh-agent://default"],
                                "allowed_agent_key_fingerprints": ["SHA256:<agent key>"],
                                "agent_public_keys": ["ssh-ed25519 AAAA... <comment>"]}}}
```

A reference is resolved only if the policy allows it for that `machine_id` **and** login `username`
(`CREDENTIAL_POLICY_NO_RULE`, `CREDENTIAL_POLICY_USERNAME_MISMATCH`, `CREDENTIAL_POLICY_REF_NOT_ALLOWED`).
Without a readable policy the probe still pins host keys but reports `AUTH_REQUIRED`
(`CREDENTIAL_POLICY_REQUIRED`) and never attempts a login. Keep the policy out of CI and of any process that
must not authenticate to machines.

| Method | `auth_ref` | Probe behaviour (`pados.machines.ssh.OpenSshProbe`) |
|---|---|---|
| `SSH_KEY` | `secretref://<provider>/<path>` | Resolved through `SecretBoundary` to an identity file path: `IdentityFile=<path>`, `IdentitiesOnly=yes`, `IdentityAgent=none`. PADOS never reads the key. |
| `SSH_AGENT` | `ssh-agent://default` | Only the policy's pinned agent keys (`CREDENTIAL_POLICY_AGENT_KEY_NOT_PINNED` otherwise): `IdentityAgent=<socket>`, `IdentitiesOnly=yes`, `IdentityFile=<pinned public key file>`, so the agent cannot offer other identities. Every pinned public key must have an allowed fingerprint. |
| `OS_CREDENTIAL_REF` | `oscred://<store>/<name>` | OpenSSH cannot use it directly: `AUTH_REQUIRED` (`OS_CREDENTIAL_REF_REQUIRES_HELPER`) until a helper is wired. |
| `PASSWORD_BOOTSTRAP_COMPLETED` | the installed key's reference | Behaves as its `key_auth_method`. |

## 5. One-time password bootstrap

`bootstrap(draft, one_time_password, transport)` (an `ssh-copy-id` style flow) through an injected
`BootstrapTransport`:

1. `presented_host_key(target)` must equal the pinned, operator-confirmed key. Otherwise the result is
   `HOST_KEY_MISMATCH` or `MACHINE_UNREACHABLE` and **the password is never sent**.
2. `install_public_key(target, pinned, one_time_password, public_key)` uses the password once to append an
   operator-provided OpenSSH **public** key (a value containing `PRIVATE` or private-key headers is refused).
3. `verify_key_login(...)` must succeed with the key reference; otherwise `BOOTSTRAP_KEY_LOGIN_FAILED`.
4. The returned profile has `auth_method=PASSWORD_BOOTSTRAP_COMPLETED`, `key_auth_method` `SSH_KEY` or
   `SSH_AGENT` and the key `auth_ref`. It is stored like any other profile.

Password isolation (CR-07 MC-05): the password is used inside one function (`_install_with_password`) that
returns a code and deletes its reference in `finally`. `bootstrap` and `ux.add_machine` drop their own
references the same way (the CLI-facing call moves it into a one-slot box emptied in `finally`) and raise a
fresh code-only `MachineError` from a frame that no longer holds it: no `__cause__`, no `__context__`, no
transport text, no traceback frame local in `pados.machines` reaching the password. `validated` likewise
deletes the raw input on every exit path (`try`/`finally`), including an exception a validator raises
without pydantic wrapping it (CR-07b MC-05). The transport protocol documents its own secret-handling contract
(reveal only at the moment of use; never store, log, serialize or put the value in an exception; never send
it before the pinned host key was verified).

What PADOS claims, precisely: the password is **not persisted**, and it is **not in PADOS files, logs,
receipts, PADOS exception text or `pados.machines` traceback locals**. It does not claim more: Python cannot
wipe immutable string memory, the caller's own references are the caller's, and a transport is bound only
by its contract. Tests use a hostile fake transport that echoes the password in its own exception and assert
the password is in no file under the test directory, no log record, no exception text and no
`pados.machines` frame of the traceback. PADOS ships the transport protocol but no password-capable SSH
transport in V1 (no new dependency); the operator supplies the public key (for example from `ssh-keygen`).

## 6. Connection states and staleness

| State | Meaning |
|---|---|
| `ONLINE` | Pinned host key proven, key authentication observed, Herdr (if enabled) observed available |
| `DEGRADED` | Reserved for probes that report a degraded observation (the OpenSSH probe never does) |
| `OFFLINE` | Not reachable (or the probe failed; probe exception text is never kept) |
| `AUTH_REQUIRED` | Pinned key proven, but authentication failed, was not attempted (policy) or was not recognized |
| `UNAUTHORIZED` | `HOST_KEY_MISMATCH` (possible spoof; sticky) or no host key proven |
| `HERDR_UNAVAILABLE` | Herdr enabled for the machine but not observed available (unknown counts as unavailable) |

`last_seen` changes only when the pinned host key was proven. `state_observed_at` drives staleness: an
observation older than `stale_after_seconds`, or with a future, naive or unparseable timestamp, is
`stale=true` and is shown as `STALE` with `last_known_state`, never as live. `UNAUTHORIZED` stays visible
even when stale. `describe` / `pados machine show` render through the same projection with the inventory's
threshold (`State: STALE (last known ONLINE, observed ...)`; CR-07 MC-07).

Observations are bound to the probed profile's revision digest over an explicit field list
(`PROBE_RELEVANT_FIELDS`: machine id, host, port, hostname confirmation, username, auth method, key auth method,
auth reference, host key and fingerprint, and the Herdr settings; CR-07b MC-11) and applied compare-and-set under
the inventory lock. A test requires `PROBE_RELEVANT_FIELDS` and `OBSERVATION_FIELDS` to partition the profile
schema, so a new field cannot ship unclassified.
If the profile changed while the probe ran, the observation is discarded with `OBSERVATION_STALE_REVISION`
and nothing is stamped (CR-07 MC-08); the same applies to `update_location`.

Probe targets come only from the operator-owned inventory (`run_connection_test` takes a machine id, never
a host). A host must be a literal RFC 1918, carrier-grade NAT overlay (100.64.0.0/10) or IPv6 unique-local
address, or a hostname the operator confirmed. Public, loopback, link-local and metadata (169.254.0.0/16),
multicast, unspecified, reserved, broadcast and IPv4-embedding IPv6 forms are refused, as are numeric
shorthands such as `10.1`. A confirmed hostname is resolved by the probe and every resolved address must pass
the same rules (DNS rebinding defence).

The ordinary connection test is **authentication only** (CR-07 MC-06). `ssh-keyscan` reads the presented
host key; a host that does not present the pinned key is never logged into. Then `ssh -N` runs with no
remote command and no session or command channel, so nothing executes through the account's shell, startup
files or a forced command. Options: `-F` set to the null device, `BatchMode=yes`,
`StrictHostKeyChecking=yes`, the PADOS-managed `UserKnownHostsFile`, `HostKeyAlias=<machine_id>`,
`ConnectTimeout`, password and keyboard-interactive authentication off, agent/X11 forwarding off,
`ClearAllForwardings=yes`, `ControlMaster=no`, `RequestTTY=no` and
`LogLevel=ERROR`. The process runs in its own process group with a minimal environment and a hard deadline.
Authentication success is an unforgeable local signal, never server text (CR-07b MC-10):
`PermitLocalCommand=yes` and `LocalCommand=echo pados-auth-<nonce>` with a fresh 128-bit hex nonce per run
(`secrets.token_hex(16)`, validated, the only interpolated value). OpenSSH runs LocalCommand on this machine
only after successful authentication and it writes to ssh's stdout, which the server cannot write to with
`-N` (no channel exists; banners go to stderr). The result is AUTHENTICATED only if stdout is exactly that
nonce line; the process group is then killed. A stale or foreign nonce or any extra stdout is
`SSH_AUTHENTICATION_SIGNAL_INVALID`; no line before the deadline is `SSH_DEADLINE_WITHOUT_AUTHENTICATION`;
both are `AUTH_REQUIRED`, never `ONLINE`. stderr (server-influenced, e.g. an SSH banner that claims
"Authenticated to") can only make the result worse. LocalCommand runs through the local user's shell
(a fixed `echo` of a hex token). Any shell-backed check would be a
`PRIVILEGED_OPERATION` behind a grant. Output is mapped to local codes and never stored. Tests never run SSH.

## 7. Privileged operations and break-glass

`TERMINAL`, `SHELL`, `ADMIN` and `FILE_TRANSFER` are `PRIVILEGED_OPERATION`s (`CONNECTION_TEST` and `STATUS`
are ordinary). An ordinary Remote Operations session (`pados.remote.auth.RemoteSession`, or its
`RemoteActor`) can never request, approve, satisfy or execute one:
`REMOTE_SESSION_CANNOT_GRANT_PRIVILEGED_OPERATION`.

**Operator authentication (CR-07 MC-01).** No caller-constructed actor is proof. The operator registry is an
owner-only file (`{"schema_version": "pados.machines.operators.v1", "operators": [{"operator_id": ...,
"proof_key_ref": "secretref://..."}]}`); proof keys are resolved only through `SecretBoundary`.

1. `OperatorAuthenticator.challenge(request)` issues a fresh challenge bound to the full `GrantRequest`
   digest (machine, operation, project, command, validated arguments), `machine_id`, `project_id` and an
   expiry (at most 5 minutes), MAC'd with the Home Base key so it cannot be invented.
2. The requester and the approver each answer with `OperatorProof(operator_id, proof)`, an HMAC-SHA256 under
   their own key over the challenge and their role (`compute_proof` is the operator-side computation).
3. `issue_grant(request, challenge=..., requester=..., approver=...)` verifies both proofs itself
   (`CHALLENGE_INVALID`, `CHALLENGE_REQUEST_MISMATCH`, `CHALLENGE_EXPIRED`, `OPERATOR_UNKNOWN`,
   `OPERATOR_PROOF_INVALID`), requires two different operators unless the inventory is `LOCAL_SINGLE_USER`,
   uses each challenge once (`CHALLENGE_ALREADY_USED`) and records only operator ids and proof digests.
   `issue_grant` is the only ISSUED writer: the ledger's public `append` accepts `DENIED` records only
   (`GRANT_LEDGER_ISSUE_REQUIRES_PROOFS` for ISSUED, `GRANT_LEDGER_TRANSITION_REQUIRES_AUTHORIZATION` for the
   rest), and the internal ISSUED writer refuses a grant whose proof digests, roles and challenge do not match
   the operators `issue_grant` verified (CR-07b MC-01).
   `LocalOperator` is the verified result type; Python cannot make it truly unconstructible, and because
   issuance re-verifies the raw proofs, forging the object is useless.

**Bound authorization (CR-07 MC-04).** `authorize_privileged(grant, *, operation, machine_id, project_id,
command_id, arguments, ...)` compares every bound field (`PRIVILEGED_GRANT_MACHINE_MISMATCH`,
`..._OPERATION_MISMATCH`, `..._PROJECT_MISMATCH`, `..._COMMAND_MISMATCH`) and the request digest
(`PRIVILEGED_REQUEST_DIGEST_MISMATCH`), checks expiry, the security profile, the machine state and the
registered command schema, and consumes the grant once. The returned `PrivilegedAuthorization` carries the
full request digest. `consume_authorization(...)` is the execution boundary and trusts nothing on the
authorization object (CR-07b MC-09): it looks up the authenticated `CONSUMED` record by authorization id and
the matching `ISSUED` record, and takes expiry, operation, machine, project, command, actor and the request
digest from the ledger. Any supplied field that differs is `PRIVILEGED_AUTHORIZATION_TAMPERED` (a copied
authorization with an extended `expires_at` is refused), expiry is checked against the ledger value
(`PRIVILEGED_AUTHORIZATION_EXPIRED`), the executed request must have the ledger digest
(`PRIVILEGED_EXECUTION_REQUEST_MISMATCH`), and each authorization executes exactly once
(`PRIVILEGED_AUTHORIZATION_ALREADY_EXECUTED`). The EXECUTED record is derived from the CONSUMED record.

* Grants live at most 15 minutes; `break_glass` grants at most 5 minutes, are flagged in the ledger and the
  authorization, and never relax approval.
* `SHELL` and `ADMIN` need a registered `PrivilegedCommand` with an argument schema (the
  `pados.worker.command` model). Nothing is registered by default, so no shell is exposed. Argument values
  are plain tokens (no shell metacharacters, no leading `-`).

**Grant ledger (CR-07 MC-02).** Append-only JSONL (`ISSUED`, `CONSUMED`, `EXECUTED`, `DENIED`). Every entry is
hash-chained and carries an HMAC under the Home Base key (SecretRef), so an `ISSUED` record cannot be
fabricated without the key. A separate owner-only head file stores (count, last entry hash, MAC) and is
verified on every read, so truncation, deletion or rollback of `CONSUMED`/`EXECUTED` records is
`GRANT_LEDGER_ROLLBACK_DETECTED`. Both files are opened descriptor-based with `O_NOFOLLOW` and must be
regular files owned by the current user with mode 0600, in a parent that is not group/world writable
(`GRANT_LEDGER_OWNER_INVALID`, `..._PERMISSIONS_TOO_OPEN`, `..._PARENT_INSECURE`, `..._OPEN_REFUSED`). Given a
trusted `root` (the inventory directory), every ancestor directory up to it must also be a real directory
(not a symlink) owned by the current user or root and not group/world writable, and the file must lie under
it (`..._OUTSIDE_ROOT`); the same applies to the operator registry and the credential policy. A crash
between the ledger append and the head update fails closed (`GRANT_LEDGER_ROLLBACK_DETECTED`) and needs
operator recovery. Records pass `assert_minimized` with an explicit schema and never contain command text,
argument values, terminal output or scrollback. ACCEPTED_RISK: an attacker who holds both files **and** the
Home Base key can rewrite the ledger consistently. ACCEPTED_RISK (CR-07b MC-02): an attacker able to replace
PADOS-owned files inside the protected directory can restore an older ledger together with its matching head
and replay a grant that was consumed after that snapshot; detecting this needs an external monotonic anchor
(a counter or timestamp the attacker cannot roll back), which V1 does not have. ACCEPTED_RISK_WINDOWS: ownership and mode checks are
POSIX-only; on Windows the profile directory ACL is relied on.

## 8. Compute nodes and Herdr

`NodeRegistration` has an optional `machine_id` (`mach-<20 hex>`); existing registries without it load
unchanged. `check_compute_nodes(inventory, nodes)` reports `COMPUTE_NODE_MACHINE_UNKNOWN`,
`COMPUTE_NODE_MACHINE_ROLE_MISSING`, `MACHINE_CLAIMED_BY_CONFLICTING_NODES` (two nodes claiming one machine
with different endpoint hosts), `MACHINE_COMPUTE_NODE_LINK_UNKNOWN` and `MACHINE_COMPUTE_NODE_LINK_MISMATCH`.
It reports and never repairs.

Herdr is a separate package; `pados.machines` never imports it. A profile carries only `herdr.enabled` and an
opaque `herdr.ref`; `OpenSshProbe(herdr_check=...)` lets the integrator plug in the Herdr availability check.

## 9. Visibility

`machine_visibility(config_path)` (the inventory path) returns `status` (`MACHINES_NOT_CONFIGURED`,
`MACHINE_INVENTORY_EMPTY`, `MACHINE_INVENTORY_INVALID`, `MACHINES_OBSERVED`),
`plaintext_password_persistence: "PROHIBITED"`, `semantic_authority: "NONE"`, `shell_exposed: false` and per
machine: `machine_id`, `display_name`, `roles`, `platform`, `connection_state`, `last_known_state`,
`last_seen`, `stale`, `identity_alert`, `host_key_fingerprint`, `auth_method`, `auth_ref_scheme`, `herdr`
(`enabled`, `ref_configured`, `ref_scheme`). It never includes the host, the username, a credential or any
reference value beyond its scheme.

## 10. Threats

| Threat | Control | Test |
|---|---|---|
| Plaintext password persisted | No field; `assert_minimized` on input and persist; code-only errors | `test_bootstrap.py`, `test_identity_profile.py` |
| Password leaked via logs / exceptions / traceback locals | Isolated use, references deleted in `finally`, fresh code-only error | `test_password_never_appears_in_files_logs_or_exceptions`, `test_cr07_mc05_*` |
| Password sent to a spoofed host | Presented key must equal the pinned key before the password is used | same, `host_key_mismatch` case |
| Hostname/IP spoofing | Identity is the host key; `HOST_KEY_MISMATCH` sticky `UNAUTHORIZED` | `test_spoofed_host_at_known_address_is_unauthorized_and_never_reenrolled` |
| Duplicate inventory | One profile per key / location / name | `test_duplicate_inventory_is_refused` |
| Arbitrary probe target / SSRF / DNS rebinding | Inventory-only targets; private literal or confirmed hostname; resolved addresses re-checked | `test_connection_targets_are_private_literals_or_confirmed_hostnames`, `test_confirmed_hostnames_must_resolve_only_to_allowed_addresses` |
| Credential-use confused deputy (inventory picks a ref) | Separate operator credential policy per machine + username; pinned agent keys | `test_cr07_mc03_*` |
| Shell code run by the ordinary connection test | `ssh -N`, no remote command, verbose-line detection, fail closed | `test_cr07_mc06_*` |
| Caller-built approval | HMAC proofs over a PADOS challenge from both operators | `test_cr07_mc01_*` |
| Ledger fabrication / replay / rollback | Owner-only files, HMAC per entry, authenticated head | `test_cr07_mc02_*` |
| Authorization reused for another project / command | Every field + request digest; execution consumed once | `test_cr07_mc04_*` |
| Copied authorization with altered metadata (expiry) | Execution reads every field from the authenticated ledger | `test_cr07b_mc09_*` |
| Server banner faking authentication | Local LocalCommand nonce on ssh stdout | `test_cr07b_mc10_*` |
| Herdr (or other probe input) changed during a probe | Explicit revision field list incl. Herdr; partition test | `test_cr07b_mc11_*` |
| Remote session escalating to a shell | Remote sessions refused everywhere; no shell by default; no route | `test_privilege.py`, `test_static_boundaries.py` |
| Stale state presented live | `STALE` presentation in visibility, list, `describe` and `machine show` | `test_stale_state_is_never_presented_live`, `test_cr07_mc07_*` |
| Observation applied to a changed profile | Revision digest, compare-and-set under the inventory lock | `test_cr07_mc08_*` |
| CI discovering credentials | Inventory holds references only; no policy file, no credential use | `test_ci_environment_cannot_discover_credentials_from_the_inventory`, `test_cr07_mc03_no_policy_means_no_credential_use` |
| Terminal output entering Memory | No Memory / state_proof / Herdr imports; ledger holds no command text | `test_static_boundaries.py`, `test_ledger_holds_no_command_text_and_chain_tampering_fails_closed` |

Residual risks (ACCEPTED for alpha): trust-on-first-use if the operator accepts a new key without comparing
the fingerprint out of band; anyone who can write the owner-only inventory can change its security profile;
anyone holding the grant ledger, its head and the Home Base key can rewrite the ledger; restoring an older
ledger with its matching head replays later-consumed grants (needs an external monotonic anchor); password
bytes may linger in process memory until garbage collection; the LocalCommand signal requires an OpenSSH
client that honours `PermitLocalCommand` with `-N` (otherwise the result fails closed to `AUTH_REQUIRED`);
`OS_CREDENTIAL_REF` needs a helper before it can authenticate.

## Operator commands

Set `PADOS_MACHINES_CONFIG` to the inventory file (owner-only), write the credential policy next to it (or pass
`--credential-policy`), then:

```bash
pados machine add --name Mac --host 192.168.x.x --user <account> --auth ssh-agent \
  --platform MACOS --role HERDR_MACHINE --fingerprint SHA256:<compare out of band>
pados machine list [--json]
pados machine show Mac          # same freshness projection: STALE (last known ...) when old
pados machine test Mac          # ssh -N: pinned host key, no session, no remote command, no password
pados machine remove Mac
```

`--accept-new-host-key` is the explicit trust-on-first-use alternative to `--fingerprint`. The CLI never accepts a
password; password bootstrap (`pados.machines.bootstrap`) needs an operator-supplied password-capable transport,
which is not shipped in V1. Doctor and Mission Control show a read-only `machines` block (states and references only).
