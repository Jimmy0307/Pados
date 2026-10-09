# Deployment security profiles

| Profile | Scope | Node identity | Control API | Status |
|---|---|---|---|---|
| `LOCAL_SINGLE_USER` | One machine, one user; loopback only | HMAC (`HMAC_SHARED_SECRET`); registration must use a loopback endpoint | Loopback plus unique control token (`pados init`) | **Supported (alpha)** |
| `TRUSTED_LAN` | Home or office private LAN, direct host-to-host links | HMAC over a SecretRef-backed credential | Loopback on the PADOS host | **Supported (alpha)**. Not Internet-facing |
| `HARDENED_ENTERPRISE` | Multi-host, remote, or untrusted networks | `ASYMMETRIC_IDENTITY` (Ed25519 / mTLS): designed, **not implemented**. Registrations fail closed (`ASYMMETRIC_IDENTITY_NOT_AVAILABLE`) | Requires SSO/RBAC (enterprise candidates) | **Not supported yet** |

**Rules**

- HMAC identity is the LAN profile. It is **not** sufficient for Internet-facing deployment, and PADOS does not recommend one.
- Nodes refuse public and wildcard binds. PADOS refuses non-private node endpoints and endpoints with embedded credentials.
- `HARDENED_ENTERPRISE` registrations reject HMAC (`IDENTITY_PROFILE_HMAC_SHARED_SECRET_NOT_ALLOWED_FOR_HARDENED_ENTERPRISE`).
- `pados doctor` reports `security_posture` (`deployment_profile`, warnings such as `DEFAULT_CONTROL_TOKEN_HASH_IN_USE`, `internet_facing_supported: false`). It never changes Core readiness.
- **Asymmetric identity.** It would ship as an optional extra (for example `pados[node-security]` with `cryptography`), not as a mandatory Core dependency. The `NodeIdentityProvider` protocol in `pados.compute_node.identity_providers` is the extension point.
