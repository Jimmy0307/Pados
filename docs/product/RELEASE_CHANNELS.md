# Release channels and recommended first public status

| Channel | Meaning | Entry criteria |
|---|---|---|
| development | Internal branches | CI green |
| **alpha** | Public preview; APIs may change; single-user/LAN only | All CI release gates; deterministic demo; threat model published; no PUBLIC_RELEASE_BLOCKER |
| beta | Feature-complete for the documented scope; upgrade path tested | No BETA_BLOCKER (e.g. default control-token hash removed; receipt freshness; TLS option for LAN nodes) |
| stable | Compatibility policy in force | Beta feedback resolved; security review; hardened profile or explicit scope statement |

**Recommendation: `0.1.0a1` (alpha).** The evidence supports alpha maturity: every gate is green, the demo is reproducible, and the governed chain is proven. It does not support calling the system production-stable, for these reasons:

- LAN-only HMAC identity;
- no transport encryption;
- a default control-token hash (warned);
- Codex on Windows blocked by an external sandbox issue;
- no hardware validation on Apple Silicon.

**Current gate:** `PUBLIC_RELEASE = NOT_READY` until the two human decisions in REMAINING_BLOCKERS.md (license, donor-derived code) are made. After that, the tree is ALPHA-ready.

**Update.** The license decision is made (`AGPL-3.0-only` core plus commercial licensing; `LICENSING.md`). Donor-derived code was replaced clean-room (`docs/product/THIRD_PARTY_PROVENANCE.md`). Publication still requires legal review of copyright ownership and the CLA, and an explicit human publication decision.
