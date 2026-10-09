# Connectivity Closure V1

This phase closes items 2, 3 and 4 of the Intermittent Connectivity V1 remaining blockers: the background
probe, the provider connectivity requirement, and durable notification delivery. It also defines the
connectivity data lifecycle. The rule from V1 still holds: **PADOS AUTHORITY MUST NOT DEPEND ON WAN
AVAILABILITY.** Everything below is an operational transport projection. It has `semantic_authority=NONE`
and `affects_job_state=false`.

| Item | Status |
| --- | --- |
| `CONNECTIVITY_BACKGROUND_PROBE` | implemented, opt-in (`background_probe.enabled`) |
| `PROVIDER_CONNECTIVITY_REQUIREMENT` | `ENFORCED` when `PADOS_CONNECTIVITY_CONFIG` is set |
| `DURABLE_NOTIFICATION_DELIVERY` | implemented (`DurableNotificationOutbox`) |
| `PHONE_HOTSPOT_PHYSICAL_UAT`, `CELLULAR_REMOTE_ACCESS_UAT` | still `PENDING` (no hardware in CI) |

## Configuration

Set `PADOS_CONNECTIVITY_CONFIG` to an operator-owned JSON file:

```json
{
  "mode": "INTERMITTENT_WAN_HOME_BASE",
  "wan_probe": {"host": "<public literal IP>", "port": 443},
  "overlay_probe": {"host": "<overlay peer literal IP>", "port": 8089},
  "snapshot_path": "<PADOS runtime dir>/connectivity.json",
  "history_path": "<PADOS runtime dir>/connectivity-history.json",
  "stale_after_seconds": 300,
  "background_probe": {"enabled": true, "interval_seconds": 60, "timeout_seconds": 3,
                       "max_backoff_seconds": 600},
  "provider_requirements": {"codex-cli": "WAN_REQUIRED"}
}
```

If the file is unset, PADOS runs normally: no thread starts, nothing is probed, and doctor, Mission Control
and `/remote/v1/connectivity` return the same `CONNECTIVITY_NOT_CONFIGURED` result as before.
`history_path` defaults to `<snapshot stem>-history.json` next to the snapshot.

## A. Background probe (`pados.connectivity.background`, `pados.connectivity.runtime`)

* `BackgroundProbe` is one daemon thread (`pados-connectivity-probe`) that owns an existing
  `ConnectivityMonitor`. It reuses the V1 state machine (`CONNECTED` / `INTERMITTENT` / `OFFLINE` /
  `RECOVERING`); there is no second one. Each tick calls `observe()` and nothing else.
* It is **not a scheduler**. It schedules no work, cancels no job, and mutates no semantic state.
* Bounds:
  * `interval_seconds` is 5 to 3600 (default 60).
  * `timeout_seconds` is greater than 0 and at most 10 per probe (default 3; the V1 default of 1.5 s
    applies when `background_probe` is absent).
  * While a link is `OFFLINE` or `INTERMITTENT`, the delay doubles each tick, from `interval_seconds` up
    to `max_backoff_seconds` (which must be between the interval and 3600).
* Shutdown is clean and bounded. The wait between ticks is `Event.wait`, so `stop()` interrupts it. A tick
  in progress lasts at most the sum of the per-probe timeouts. `stop()` therefore joins for
  `timeout_seconds x probes + 2 s` by default, is idempotent, and returns `False` if the thread is still
  alive. `run_once()` and an injectable clock and waiter make tests deterministic.
* Each tick also runs retention maintenance for the connectivity history store (`history.prune`). This is
  upkeep of the connectivity store only, not scheduling of work. A maintenance failure is recorded in
  `last_error`, and the probe keeps running.
* `probe.health()` reports `alive`, `last_success_at` and `last_error`.
* Ownership: the runtime stays registered as `stopping` until its thread has actually exited. While it is
  stopping, readers fall back to their own monitors, and `start_runtime` raises
  `CONNECTIVITY_RUNTIME_STILL_STOPPING`. The lifespan logs a stop that did not finish, or a refused start;
  PADOS itself still starts.
* `pados.connectivity.runtime` holds the **single process-wide runtime**. The API (`pados.api.app`) and the
  overlay app (`create_remote_app`) start it in their FastAPI lifespan and stop it at shutdown. While it
  runs, these all read the same monitor:
  * `pados doctor` (`probe_source=BACKGROUND_PROBE`);
  * Mission Control (`connectivity_visibility`);
  * `/remote/v1/connectivity` and `/remote/v1/home-base` (`connectivity_reader`);
  * the provider gate (`ConfiguredWan`).
* Every view (doctor, Mission Control, `/remote/v1/connectivity`) carries `observation_age_seconds`,
  `stale`, `probe_mode` and `probe_thread` (health, or `null` when there is no thread).
  * If the shared view is stale, doctor refreshes it once (`BACKGROUND_PROBE_REFRESHED`).
  * A doctor run in another process uses the daemon's snapshot only while it is fresh
    (`BACKGROUND_SNAPSHOT`); otherwise it probes directly (`DIRECT_PROBE`).
  * Reachability is never derived from stale data: doctor reports `remote_status=REMOTE_STATUS_STALE`.
* Observation timestamps (F-22): an `observed_at` more than 60 s in the future, timezone-naive or
  unparseable has no age and is treated as **stale**. It is never fresh and never raises. The same rule
  applies to Mission Control, doctor, the runtime view and the provider gate. Such a future-dated
  snapshot also cannot block newer snapshot writes.
* **Coherent observation.** `ConnectivityMonitor.observation()` returns one immutable `Observation`.
  Its snapshot, per-link states and age are taken together under the state lock, and the age is derived
  from that snapshot's own `observed_at`. The provider gate (`ConfiguredWan`), the runtime view, the
  remote route and doctor all use it. State and age are never combined from separate reads.
* Persistence is ordered. `observe()` is serialized by an observation lock, so snapshot and history
  writes happen in observation order. Readers take a short state lock and always see the last complete
  observation; a slow probe never blocks them. The snapshot file is never overwritten by an older
  observation from any process, and every snapshot carries a `sequence` number.
* **SSRF defence.** Probe targets come only from the operator file. No API or remote client can set one.
  They are validated when the file is loaded:
  * The host must be a literal IP, never a hostname, so there is no DNS rebinding.
  * The port must be 1 to 65535.
  * These addresses are refused: loopback; link-local (169.254/16, including the 169.254.169.254
    metadata address, and fe80::/10); multicast; unspecified; broadcast; reserved; and IPv6 forms that
    embed IPv4 or carry a zone ID.
  * The WAN probe must classify `PUBLIC` and be globally routable.
  * The overlay probe must classify `PRIVATE_OVERLAY` (both checks use `classify_bind_address`).
  * Any violation makes the file `CONNECTIVITY_CONFIG_INVALID`, with a reason code that never contains
    the IP. Nothing is probed.

## B. Provider connectivity requirement (`pados.connectivity.requirements`, `maker`, `provider_gate`)

* `NetworkRequirement` is one of `LOCAL_ONLY`, `PRIVATE_NETWORK`, `WAN_REQUIRED` or `NETWORK_OPTIONAL`.
* The requirement is derived from metadata, never from provider names. Sources are checked in this order:
  1. Explicit operator metadata: `provider_requirements[provider_id]`.
  2. Endpoint locality: loopback gives `LOCAL_ONLY`; RFC 1918, 100.64/10 and fc00::/7 give
     `PRIVATE_NETWORK`; anything else gives `WAN_REQUIRED`.
  3. `ProviderCapabilityProfile.locality` from `profile_for_worker`: `LOCAL` gives `LOCAL_ONLY` and
     `REMOTE` gives `WAN_REQUIRED`.
* If metadata is missing, the result is `WAN_REQUIRED`.
* An invalid value, or a `REMOTE` profile declared `LOCAL_ONLY` or `NETWORK_OPTIONAL`, fails closed with
  `PROVIDER_CONNECTIVITY_METADATA_INVALID`. The result is `BLOCKED` (`AUTHORITY_BLOCK`, not retryable),
  and nothing executes.
* `RequirementGatedMaker` checks the requirement before dispatch. `WAN_REQUIRED` needs a **fresh** WAN
  observation: its age must be at most `stale_after_seconds`, and it must not be `UNKNOWN`.
  * If the observation is missing, stale or untrusted (F-22), the gate first runs exactly one bounded
    synchronous refresh. This applies to the shared background monitor too.
  * The gate returns the existing failed `RuntimeResult` (`PROVIDER_UNREACHABLE`, `RUNTIME_FAILURE`,
    retryable), and never calls the inner maker, in these cases:
    * the observation is still not fresh (`WAN_OBSERVATION_STALE`);
    * WAN is `OFFLINE` or `INTERMITTENT` (`WAN_UNAVAILABLE`);
    * a connectivity config is set but has no `wan_probe` (`WAN_NOT_OBSERVABLE`).
  * The reason codes above are internal diagnostics (`maker.diagnostics`, in memory only).
  * Only the wholly unconfigured path (no `PADOS_CONNECTIVITY_CONFIG`) keeps the ungated behaviour.
  * `LOCAL_ONLY`, `PRIVATE_NETWORK` and `NETWORK_OPTIONAL` dispatch without WAN.
  * The gate never selects or falls back to another provider or model.
* Default wiring: when `PADOS_CONNECTIVITY_CONFIG` is set, `build_product_loop_service` wraps the
  `CodexCliMaker` in `gate_product_loop_maker`.
  * The requirement comes from the worker that THE Worker Router (`select_product_loop_provider`, then
    `select_worker`) selects for the product loop. That worker's `profile_for_worker` metadata decides it
    (codex-cli is `REMOTE`, so `WAN_REQUIRED`).
  * If the config is invalid, every dispatch is `BLOCKED` with `CONNECTIVITY_CONFIG_INVALID`.
  * If the config is unset, the maker is unchanged.

## C. Durable notification delivery (`pados.remote.outbox`)

* `DurableNotificationOutbox` is a PADOS-owned file, written atomically (temp file, fsync, replace) with
  mode 0600. Its path is `RemoteConfig.notification_outbox_path`. The optional
  `notification_outbox_path` key overrides the default, which is `notification-outbox.json` next to the
  ledger.
* Each record stores only these fields: `notification_id`, `kind`, `ref` (the opaque `push_metadata` hash,
  never a project name or goal), `created_at`, `attempts`, `last_attempt_at`, `next_attempt_at`,
  `delivery_status`, `expires_at` and `provider_ref`. Every record passes the data-minimization guard with
  this explicit key schema.
* Data kind is `NOTIFICATION_DELIVERY` (`R1_OPERATIONAL`). Records expire after `RetentionPolicy().r1`
  (14 days), or sooner if a shorter period is configured, never later.
* Every operation reloads, modifies and persists under a **cross-process store lock**: `fcntl.flock` on
  POSIX and `msvcrt.locking` on Windows, on a sidecar `<file>.lock`, plus an in-process lock. Several
  processes or instances can therefore share one outbox without lost updates.
* Records are strictly validated on enqueue **and** on reload. Each of these must hold:
  * `kind` is a `NotificationKind`;
  * `ref` is 16 lowercase hex characters;
  * `notification_id` is `nf-` followed by 32 hex characters;
  * every timestamp is ISO-8601 with a zero UTC offset and within sane bounds;
  * `attempts` is an int between 0 and 10000;
  * the status is a known value;
  * `next_attempt_at <= expires_at <= created_at + R1`.

  A record that fails any check is dropped and counted (`corrupt_dropped`). It is never forwarded.
* `NotificationCenter(push, durable=outbox)` behaves as follows:
  * A record is persisted as `PENDING` before the first send.
  * Pending records are reloaded when the center is constructed, so they survive a Home Base restart.
  * Each individual send is immediately preceded by its own atomic **claim**. `flush()` claims one record
    at a time and never claims a batch. A claim re-checks expiry and backoff under the store lock, then
    persists, before the send:
    * status `IN_FLIGHT` and `attempts` + 1;
    * a random 32-hex `attempt_id`;
    * a lease `lease_until = now + lease` (default 2 min). The lease is independent of the retry backoff,
      and the owner can renew it for long sends.
    * `next_attempt_at = now + backoff`, used for a later retry.
  * `complete()`, `fail()` and `renew()` must present the matching `attempt_id`. A stale owner's call is a
    no-op that returns `STALE_ATTEMPT`, so it can never reset a newer attempt's claim.
  * An `IN_FLIGHT` record becomes claimable again only after its lease elapses (the owner crashed or
    overran). A failed attempt becomes due again after the backoff.
  * Both `emit` and `flush` honour `next_attempt_at` and `expires_at`. A repeated emit cannot bypass the
    backoff, and an expired record is never sent.
  * Expired records are reported `EXPIRED` and removed. Delivered records are purged after expiry.
  * Delivery is **at-least-once with an idempotency key**. The metadata-only push payload carries the
    opaque `notification_id` (it is in `PUSH_ALLOWED_KEYS`). If the process crashes after a send but
    before the acknowledgement is written, or a send outlives its lease, the record is claimed again
    after `lease_until`. It is re-sent with the same key. A record already `DELIVERED` is never pushed
    again.
  * **Push provider contract:** the provider (or receiver) MUST deduplicate on `notification_id`. PADOS
    guarantees at-least-once delivery with a stable idempotency key; it cannot enforce deduplication
    inside the provider. A provider that does not deduplicate may show a duplicate metadata-only push.
    This is an **ACCEPTED_RISK**: the duplicate carries no content and triggers no semantic operation.
  * Retention is enforced when the outbox is opened (Home Base start), on every operation, on read, and on
    every `flush()`, even when nothing is due. Expired records are removed from disk under the store lock.
  * The validators check that a field is a string before testing its membership in an allowed set.
    Malformed JSON values (lists or objects in enum fields) are therefore dropped and counted, and never
    raise. An `IN_FLIGHT` record must carry `attempt_id` and `lease_until`; any other record must have
    neither.
  * The outbox only ever calls the push sender with a metadata-only payload (`PUSH_ALLOWED_KEYS`). It
    imports nothing from service, approval, live or state_proof code, and delivery status never touches
    job state.
* Callers without an outbox keep the V1 in-memory behaviour unchanged.

## D. Connectivity data lifecycle

| Data | Class | Storage |
| --- | --- | --- |
| Current projection | operational state | snapshot file, overwritten atomically (0600) |
| Connectivity history (`CONNECTIVITY_HISTORY`) | `R1_OPERATIONAL` | `{link, from, to, at}` transitions only; bounded by count (default 500) and by R1 age; retention enforced on write, on read (`entries()` never returns expired or malformed rows and removes them from disk), at runtime startup and on every background tick; cross-process locked; minimization-guarded |
| Probe debugging (`PROBE_DEBUG`) | `R0_EPHEMERAL` | in-memory deque (64 entries) of link states and delays; never persisted |

These are never persisted: SSIDs, public or mobile IP history, probe target IPs, network credentials,
WireGuard or Tailscale keys. A test checks that no snapshot, history, doctor, Mission Control or route
output contains a probe target IP.

## E. Security tests (`tests/connectivity/test_connectivity_security.py`)

The probe cannot mutate semantic authority. This is checked statically (no imports of state_proof,
product_loop.state, memory, remote service, approval or live code, the DB or sqlalchemy) and by
behaviour (probe ticks leave versions, commits, receipts and work requests unchanged). The tests also
show the following:

* Remote clients cannot supply probe targets.
* Reconnect still requires authentication: missing, stale or session-id credentials get 401.
* Reconnect does not replay an approval: the original ledger outcome is returned and decide and commit
  each run once.
* Reconnect does not duplicate a job: `submit_task` runs once.
* Notification retry triggers no semantic operation.
* A provider outage never triggers a fallback: only the router-narrowed product-loop adapter is
  considered and nothing executes.

Other tests: `tests/connectivity/test_background_probe.py`, `tests/connectivity/test_provider_requirement.py`,
`tests/remote/test_notification_outbox.py`.

The regression tests for Codex review CR-03 and security finding F-22 are in
`tests/connectivity/test_cr03_connectivity.py` (`test_cr03_cc01/02/06/07/08/10_*` and `test_f22_*`),
`tests/connectivity/test_provider_requirement.py::test_cr03_cc02_*` and
`tests/remote/test_notification_outbox_cr03.py` (`test_cr03_cc03/04/05/09/10_*`).

The CR-03b verification regressions are in `tests/connectivity/test_cr03b_connectivity.py` and
`tests/remote/test_notification_outbox_cr03b.py` (`test_cr03b_*`).
