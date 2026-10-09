# Security policy (alpha)

## Supported versions

Only the latest `0.1.x` alpha receives fixes. There is no LTS or production support.

## Reporting a vulnerability

Please report privately. The contact channel will be published with the repository (placeholder: `security@<project-domain>`). **Do not** open public issues for vulnerabilities. Include affected version, reproduction steps and impact. We aim to acknowledge reports within 7 days; alpha timelines are best-effort.

## Deployment scope

PADOS 0.1 supports these security profiles only:

- `LOCAL_SINGLE_USER`: loopback;
- `TRUSTED_LAN`: private network; compute-node identity uses a shared-secret HMAC; no transport encryption.

**Do not expose PADOS, its API or compute nodes to the public Internet.** See `docs/security/THREAT_MODEL_V1.md` and `docs/security/SECURITY_PROFILES.md` for mitigations and residual risks.

## Hardening checklist

- Run `pados init` to generate a unique control token. `pados doctor` warns while the built-in default is in use.
- Keep credentials as SecretRefs (`secretref://env/NAME`); never commit `.env`.
- Register and authorize compute nodes explicitly; keep RESTRICTED data on explicitly RESTRICTED local registrations.
