# Intermittent Connectivity / Offline-First V1

Formal rule: **PADOS AUTHORITY MUST NOT DEPEND ON WAN AVAILABILITY.**

## Connectivity states (`pados.connectivity.state`)

`CONNECTED`, `INTERMITTENT`, `OFFLINE`, `RECOVERING` (plus `UNKNOWN` before the first probe), tracked per link:
`WAN` (general Internet egress: cloud providers, push) and `REMOTE_TRANSPORT` (the private overlay remote
clients use). Transitions: failure → `OFFLINE`, or `INTERMITTENT` after repeated flaps in a window; first
success after an outage → `RECOVERING`; stable successes → `CONNECTED`. Each link records
`last_connected`, `last_disconnected` and `recovered_at`.

These are operational projections only: `semantic_authority=NONE`, `affects_job_state=false`. They never
change project truth, never cancel jobs and are never semantic/audit events.

## Behaviour contract

| Situation | Behaviour | Proof |
| --- | --- | --- |
| WAN / overlay lost | local execution, receipts, artifacts, audit, ScopeVersion and CAS continue; remote console unavailable | Scenario A (`tests/connectivity/test_offline_scenarios.py`, `tests/integration/test_offline_continuity_db.py`) |
| Remote client disconnects | job continues; disconnect is never `CANCEL` | Scenario A, `RemoteJobController.on_client_disconnect` |
| Reconnect | authenticate → fetch current authoritative projection; browser history is never used | Scenario A, `e2e/remote-console-reconnect.spec.ts` |
| New remote intent while disconnected | cannot be submitted; there is no Internet queue and no claimed offline delivery | adapter raises `TransportUnavailable` |
| Local UI / CLI / trusted operator while offline | continue under normal authorization | Scenario A (local run while offline) |
| Cloud provider while WAN offline | `PROVIDER_UNREACHABLE` (failed RuntimeResult, `RUNTIME_FAILURE`, `retryable=true`); no implicit local fallback; no commit | Scenario B |
| Local compute node while WAN offline | continues; loopback/private endpoints are never WAN-gated | Scenario C (`tests/compute_node/test_node_without_wan.py`) |
| Repeated disconnect/reconnect | retried `intent_id` replays the original outcome (no new work request, approval or CAS); reused id with a different request → `INTENT_ID_REUSED` | Scenario D |
| Job reaches waiting approval while nobody is connected | stays waiting; no auto-approval or auto-commit | Scenarios A, D |
| Artifact completed while client offline | reconnect returns the original receipt id, content hash, manifest hash and completion time; nothing is regenerated | Scenario E |
| Push / WAN unavailable | notification stays `PENDING` locally and is retried by `flush()`; `NOTIFICATION_DELIVERY_STATUS` is separate from `JOB_STATUS` | `test_notification_delivery_failure_is_not_job_failure` |

## Components

* `pados.connectivity.adapter.SimulatedConnectivity`: deterministic CI adapter for `CONNECTED`, `OFFLINE`,
  flapping, reconnect, provider-only outage and remote-client-only disconnect. There is no OS firewall manipulation.
* `pados.connectivity.egress.WanEgressGate` and `pados.connectivity.maker.ConnectivityGatedMaker`: these classify
  provider endpoints as `LOCAL` (loopback, RFC 1918, 100.64/10, fc00::/7) or `CLOUD`, and fail closed for cloud
  endpoints while WAN is down.
* `PADOS_CONNECTIVITY_CONFIG` (optional): mode, TCP probe targets and snapshot path. `pados doctor` reports a
  `connectivity` section that separates `REMOTE_UNREACHABLE` (transport) from `PADOS_UNHEALTHY` (authority) and
  shows local compute status independently of WAN. Mission Control shows the last snapshot and marks it `stale`
  when it is old.
* `GET /remote/v1/connectivity`; `/remote/v1/home-base` reports `home_base=RUNNING` plus the connectivity projection.
* Remote Console banner: `HOME BASE ONLINE / REMOTE CONNECTED`, `HOME BASE RUNNING / WAN OFFLINE`, `RECONNECTING`,
  and `DISCONNECTED`. A transport problem is never shown as Home Base failure. The last view is kept in memory only
  and labelled not live; nothing is cached for offline browsing.

## Connectivity Closure V1

The background probe, the provider connectivity requirement (enforced in the default product-loop wiring
when configured), durable notification delivery and the connectivity data lifecycle are described in
`docs/remote/CONNECTIVITY_CLOSURE_V1.md`. Probe targets must now be literal IPs: a PUBLIC WAN probe and
a PRIVATE_OVERLAY overlay probe. Loopback and link-local targets are refused (`CONNECTIVITY_CONFIG_INVALID`).

## Not validated

`PHONE_HOTSPOT_PHYSICAL_UAT`, `CELLULAR_NETWORK_UAT` and `REAL_EXTERNAL_RECONNECT_UAT` = `NOT_EXECUTED`.
See `runtime/artifacts/reports/intermittent-connectivity-v1/PHYSICAL_UAT_PENDING.md`.
