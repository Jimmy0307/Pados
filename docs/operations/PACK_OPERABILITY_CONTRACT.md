# Pack and adapter operability contract (OP-009)

Code: `src/pados/capability/pack_operability.py`. Tests: `tests/capability/test_op009_pack_operability.py`.

A capability pack is operable when an operator can tell, from its sealed manifest alone, how long it may run, what
it needs, what it may reach, which failures are worth retrying and what evidence it leaves. The v1 manifest
(`pados.capability-pack.v1`) already carries these facts. They are inside the manifest digest, so they cannot change
without a new registration.

| Fact | Manifest field | Enforced by |
|---|---|---|
| Timeout | `runtime.timeout_seconds` | schema (1..max) |
| Resources | `runtime.device`, `adapters[].resources` | schema |
| Network and egress | `runtime.network`, `security.egress`, `security.egress_hosts` | schema and coherence checks |
| Retry classification | `failure_codes[].retryable` | schema, plus this contract |
| Evidence | `evidence_requirements` (always includes the core keys) | schema |
| Output validation | `verification_hooks` | this contract: every output contract has a checker |
| Health | governed adapter `available()` | this contract: a bool, never raises |

Cross-pack rules (`check`):
- `OUTPUT_UNCHECKED`: every capability's output contract has a deterministic checker.
- `RETRYABLE_REFUSAL`: a code that says the input or the caller is wrong (`INVALID`, `UNAUTHORIZED`, `STALE`,
  `MISMATCH`, ...) is never retryable, so a retry never re-asks a refusal.
- `RETRYABLE_CODE_NOT_TRANSIENT`: a retryable code is transient-shaped (`TIMEOUT`, `UNAVAILABLE`, `BUSY`,
  `RATE_LIMITED`, ...).

`profile` is the operator summary read from the manifest. It includes the digest it was read from and carries
`semantic_authority: NONE`. Registration, grants and the executor stay authoritative.

## Adding a pack

1. Declare the manifest with a checker for every output contract and honest retryable flags.
2. Add the manifest to `PACKS` in `tests/capability/test_op009_pack_operability.py`. A test fails if a module that
   declares a `CapabilityPackManifest` is missing from that list.
3. Expose health through `available()`, which returns `False` (never raises) when its runtime or provider is
   unreachable.

## Not done (needs a schema decision)

The v1 manifest has no retry *limit*, no backoff hint and no health probe interval. Adding them means a manifest
schema bump (v2). That changes every pack digest and needs re-registration, so it is an owner decision, not a
remediation step. Until then, retry limits come from the executor's own bounded policy (OP-004). The older
pre-manifest adapters (OCR, extraction, retrieval, local inference) are covered by their adapter descriptors and the
OP-005 failure-injection tests, not by this contract.
