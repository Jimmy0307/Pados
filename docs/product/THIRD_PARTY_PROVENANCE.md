# Third-party and donor provenance (public summary)

**Status:** donor-derived implementation `CLEAN_ROOM_REIMPLEMENTED`. Third-party provenance: see `THIRD_PARTY_NOTICES.md` and the license audit.

## Background

During an early consolidation phase, 16 components in 8 files were adapted from earlier private projects. Those projects carry non-public licenses, so their adapted code could not be published. Detailed internal inventories are kept outside the public export.

## Clean-room replacement

The 8 files were replaced by a clean-room reimplementation:

- `src/pados/execution/context.py`, `handoff.py`, `receipts.py`
- `src/pados/product_loop/state.py`
- `src/pados/knowledge/identity.py`, `adapters.py`
- `web/src/resourceCache.ts`, `useResource.ts`

The process ran in three stages:

1. **Contracts.** Behavioural contracts (`docs/clean-room/*_contract.md`) were written from public interfaces, callers, tests and persisted formats.
2. **Implementation.** A separate implementer with no prior exposure wrote the new code inside a quarantined export. The export had no repository history, no compiled caches, no old implementation bodies and no donor material.
3. **Similarity audit.** The replacements were audited against both the old implementation and the donor sources. Donor similarity: no flags. Old-implementation overlap: limited to contract-mandated interfaces and persisted formats, reviewed by hand.

## Other provenance facts

- Byte-identical copies: no substantive tracked file is byte-identical to any donor file (whole-tree blob comparison).
- No company, customer or HR data; no private credentials.
- Later phases (Enterprise Capability Pack, Local Compute Node, Productization) re-implemented only generic concepts; no donor code was copied.
- No third-party source is vendored or modified. Dependencies keep their own licenses (`THIRD_PARTY_NOTICES.md`).
