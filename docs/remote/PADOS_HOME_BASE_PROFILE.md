# PADOS Home Base Profile

The Home Base is the always-on machine that runs PADOS authority: the API, both
database lanes, the object store, and optionally local compute nodes. Remote
devices reach it over a private overlay. The profile is hardware- and OS-neutral:
any always-on host that runs PADOS Core (Linux, Windows, or macOS) qualifies.

## Profile

| Concern | Requirement |
| --- | --- |
| Uptime | always-on host, auto-start of PADOS services after reboot |
| Authority | PADOS API bound to localhost; overlay-facing `create_remote_app` bound to the overlay IP only |
| Databases | loopback only; never exposed remotely (guard: `DATABASE_MUST_NOT_BE_REMOTELY_EXPOSED`) |
| Credentials | non-default control token hash and DB password (guard fails closed otherwise) |
| Model servers | loopback or node-private; never exposed to remote users |
| Storage | staging, ledger, and object roots on local disk with backups |
| Updates | pull and verify releases locally; there is no remote update endpoint |
| Observability | `GET /remote/v1/home-base` reports version, network model, guard status, scanner status, and ledger integrity |

## Bring-up checklist

1. Install PADOS Core and pass `pados doctor`.
2. Set a non-default `PADOS_CONTROL_API_TOKEN_HASH` and `PADOS_DB_PASSWORD`.
3. Join the host to the private overlay (see `PRIVATE_OVERLAY_DEPLOYMENT.md`).
4. Write `PADOS_REMOTE_CONFIG` with hashed actor proofs.
5. Start `create_remote_app` on the overlay IP, then verify `/remote/v1/home-base` from a phone on the overlay.

## Connectivity profiles

**PADOS AUTHORITY MUST NOT DEPEND ON WAN AVAILABILITY.** Internet is transport, not authority.
See `INTERMITTENT_CONNECTIVITY_V1.md` for the behaviour contract.

### `INTERMITTENT_WAN_HOME_BASE`

| Characteristic | Behaviour |
| --- | --- |
| Power | Home Base stays powered |
| WAN | may disappear for hours |
| Local work | continues: project state, both DB lanes, Memory Authority, Context Resolver, local compute nodes, local inference, sandbox jobs, receipts, audit, ScopeVersion/CAS for locally authorized operations |
| Remote access | unavailable while the overlay is down; no queue, no offline phone-to-Home-Base delivery |
| Recovery | remote API, authentication, job states, offline-completed artifacts, waiting approvals, node health and failures reappear from local authority; no semantic merge is needed |
| Requirements | no permanent Internet, no public IP, no port forwarding |

### `PHONE_HOTSPOT_TEMPORARY_WAN` (documented profile, not physically validated)

| Phone | Effect |
| --- | --- |
| present (hotspot on) | WAN available; overlay can form; remote console usable |
| removed | WAN unavailable; Home Base continues local execution; cloud-provider jobs fail closed with `PROVIDER_UNREACHABLE` |
| returns | WAN recovery; remote client re-authenticates and fetches the current authoritative projection |

`PHONE_HOTSPOT_PHYSICAL_UAT = NOT_EXECUTED` and `CELLULAR_NETWORK_UAT = NOT_EXECUTED` — the hardware and
network environment are not currently available. Only deterministic simulation has been performed.

Set `mode` in `PADOS_CONNECTIVITY_CONFIG` to `INTERMITTENT_WAN_HOME_BASE` or `PHONE_HOTSPOT_TEMPORARY_WAN`
to label the deployment in doctor and Mission Control.
