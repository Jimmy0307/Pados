# Contributor License Agreement: recommendation

> **NOT LEGAL ADVICE. NOT A BINDING AGREEMENT. REQUIRES LEGAL REVIEW.**
> This document is an engineering recommendation that prepares a decision for the copyright holder and their
> counsel. It contains no agreement text and creates no rights or obligations for anyone. No contributor should
> sign or rely on anything based on this page.

## Context

- PADOS Core is licensed `AGPL-3.0-only` (owner decision OD-1).
- The copyright holder wants to keep the ability to offer the same code under a separate commercial license
  (dual licensing, OD-1/OD-4).
- Dual licensing works only if the licensor holds sufficient rights in **every** line of the code it relicenses.
  Today all PADOS-owned code originates from the project itself (see `THIRD_PARTY_NOTICES.md`); external
  contributions would change that.

## Why the DCO alone is insufficient

The Developer Certificate of Origin 1.1 (`Signed-off-by`) is a certification of provenance: the contributor states
that they have the right to submit the change **under the open-source license indicated in the file**. It is a
good provenance control, but:

1. **It grants no license beyond the project license.** Under a DCO the inbound grant is effectively
   "inbound = outbound": the project receives the contribution under AGPL-3.0-only, exactly like everyone else.
   AGPL-3.0 does not permit the recipient to relicense the contribution under proprietary or commercial terms.
2. **Commercial relicensing would then need each contributor's later consent.** Contributors can become
   unreachable, change employers or refuse, which would force rewriting or removing their code before any
   commercial license could cover it.
3. **It does not address employer rights explicitly.** A contributor's employer may own code written in the course
   of employment; the DCO relies on the individual's statement and offers no corporate-level grant.
4. **It contains no patent license running to the licensor for non-AGPL use.** AGPL-3.0 section 11 patent terms
   apply to AGPL distribution; a commercial licensee would not be covered by them.

The DCO remains useful **in addition** to a CLA, as a per-commit provenance record.

## What an inbound agreement needs to cover (for counsel to draft)

Either a broad **license grant** (preferred by many contributors because they keep their copyright) or a
**copyright assignment** (with a license back). Whichever form counsel chooses, it should address:

| Topic | Purpose |
|---|---|
| Parties | Individual CLA and corporate (entity) CLA, so employer-owned contributions are covered by the employer |
| Copyright license | Perpetual, worldwide, non-exclusive, royalty-free, irrevocable license to reproduce, modify, distribute, sublicense **and relicense under terms other than AGPL-3.0, including commercial terms** |
| Patent license | Grant covering contributor patents necessarily infringed by the contribution, for all licensing paths, with a defensive termination clause |
| Representations | Original work or right to submit; disclosure of third-party material and its license; employer permission where required |
| Moral rights | Waiver or non-assertion where the law allows |
| Outbound commitment (optional, builds trust) | A promise that contributions will also remain available under an OSI-approved license (for example the AGPL) |
| No obligation to use | The project may decline or remove contributions |
| Successors | Who holds the rights if the project or copyright holder changes (assignment to a successor entity) |
| Governing law, signature and record keeping | Electronic signing process, storage of signed agreements, mapping of GitHub accounts to signers |

Well-known public CLA templates (for example Apache-style individual/corporate CLAs, or the Harmony agreements
with the "any license" outbound option) can serve as starting points for counsel; they must be adapted, not
copied unreviewed.

## Process recommendation

1. Counsel drafts the individual and corporate CLA texts and the signing process.
2. Choose a signing mechanism that records the signer, the agreement version and the GitHub identity (a CLA bot or
   an equivalent manual register).
3. Enforce in CI: a pull request from an external contributor may merge only when every commit author has a
   recorded signature for the current agreement version. Keep DCO sign-off as an additional provenance check.
4. Update `CONTRIBUTING.md` from "not yet accepted" to the published process.
5. Keep internal contributors (the copyright holder, employees or contractors) under written agreements that
   confirm the copyright holder's rights.

## Status

`CLA = NOT_PUBLISHED`. Until a reviewed CLA and process are published, external code contributions are not
accepted (see `CONTRIBUTING.md`).

## Open legal-review items

- Identity of the copyright holder for existing code and any employer or contractor rights in it.
- Choice between license grant and assignment, and the outbound commitment.
- Jurisdiction-specific validity of electronic signatures, moral-rights waivers and assignments.
- Treatment of contributions to a future Apache-2.0 `sdk/` area (an Apache-style CLA may suffice there).
