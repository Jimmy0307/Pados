# Private Overlay Deployment

Network model: **PRIVATE_OVERLAY_ONLY**. Direct public Internet exposure is
**DIRECT_PUBLIC_INTERNET_UNSUPPORTED** in V1.

## Supported shapes (vendor-neutral)

* A WireGuard tunnel between the phone and the Home Base.
* A Tailscale-like mesh overlay (addresses in 100.64.0.0/10).
* A trusted LAN (RFC 1918) for at-home use.

In all three shapes, the overlay provides device authentication and encryption;
PADOS adds actor authentication, project allowlists, and the fail-closed deployment guard.

## Rules

* Do not configure router port forwarding. V1 never requires it.
* Start the overlay process only with `python -m pados.remote.serve`. It binds exactly the configured
  overlay address, never `0.0.0.0` or `::`, and attests the listening socket. A plain
  `uvicorn --factory pados.remote.app:create_remote_app --host <overlay-ip>` launch is no longer
  supported for overlay access: without the launcher's attestation, remote routes answer only on
  loopback.
* Keep the remote ledger and staging directories private to the PADOS service account (mode 0700).
* The guard accepts only loopback, RFC 1918, 100.64.0.0/10, and fc00::/7 literal
  addresses. Everything else, including documentation ranges, is treated as public
  and refused.
* Keep the PADOS main API, the DBs, and the model servers on loopback.
* TLS: the overlay already encrypts traffic. If a browser needs a secure context
  for the PWA service worker, terminate TLS on the overlay address with a locally
  trusted certificate.
