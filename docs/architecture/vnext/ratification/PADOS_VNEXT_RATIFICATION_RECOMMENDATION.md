# PADOS vNext Ratification Recommendation

Status: `HUMAN DECISION REQUIRED`.

## Option A: Approve foundational ADRs only

- Scope: ADR-001, 002, 003, 004, 008 and 013, subject to their listed conditions.
- Benefits: fixes authority, data ownership, Context boundary, project isolation and Obsidian role before execution expansion.
- Risks: provider, MCP, token and Memory decisions remain intentionally unratified.
- Implementation implication: permits only Architecture Ratification closeout and future boundary-evidence planning; it does not authorize Phase B code.
- P6.11 impact: none; protected compatibility baseline.

## Option B: Approve foundational ADRs plus conditional execution/provider ADRs

- Scope: Option A plus ADR-005, 007, 009, 010, 014 and 015 under conditions.
- Benefits: more complete future technical direction and fewer repeated design reviews.
- Risks: may be interpreted as premature authority for execution, MCP or deployment work before evidence exists.
- Implementation implication: still requires separate Phase approvals, but governance burden is higher.
- P6.11 impact: none if no code work is authorized.

## Option C: Defer ratification pending more evidence

- Scope: preserve all ADRs as proposed.
- Benefits: zero design lock-in while provider and legacy-flow evidence is collected.
- Risks: current ambiguity around Context, execution authority and provider subscriptions persists.
- Implementation implication: no Phase-B planning mandate.
- P6.11 impact: none.

## Recommended choice: Option A

Approve the foundational direction only, with ADR-001 and ADR-004 explicitly conditional. Defer ADR-006 and ADR-011 for evidence, and ADR-012 to Phase 2. This establishes PADOS control-plane and data sovereignty without accidentally approving provider automation, token accounting, MCP server work, GBrain adoption or a P6.11 refactor.

## Requested board decision

Record one of: `OPTION_A_FOUNDATIONAL_ONLY`, `OPTION_B_FOUNDATIONAL_PLUS_CONDITIONAL`, or `OPTION_C_DEFER` and separately identify any ADR condition that must be changed. Only an explicit subsequent decision may update ADR status from `PROPOSED`.

## A2 recorded outcome

Human governance selected `OPTION_A_FOUNDATIONAL_ONLY` on `2026-07-13`.

- Accepted: ADR-002, ADR-003, ADR-008 and ADR-013.
- Conditionally ratified while retaining `PROPOSED`: ADR-001 and ADR-004.
- Remaining proposed: ADR-005, ADR-006, ADR-007, ADR-009, ADR-010, ADR-011, ADR-012, ADR-014 and ADR-015.
- Phase B remains unapproved. CR-01 through CR-05 remain unresolved and blocking for their applicable future work.
