# PADOS vNext Ratification Decision

Decision ID: `PADOS-VNEXT-RATIFICATION-A2-OPTION-A`  
Decision Date: `2026-07-13`  
Selected Option: `OPTION_A_FOUNDATIONAL_ONLY`  
Human Decision: `FOUNDATIONAL RATIFICATION RECORDED`  
Decision Authority: `Human`  
Implementation Authorization: `NOT_GRANTED`

## Accepted ADRs

- ADR-002: PostgreSQL as System of Record
- ADR-003: Modular Monolith First
- ADR-008: Project Isolation and Repository Boundary
- ADR-013: Obsidian as Human Workspace and Projection Layer

## Conditionally accepted ADRs

ADR-001 and ADR-004 retain `Status: PROPOSED` because the repository vocabulary has no `ACCEPTED_WITH_CONDITIONS` status. Their conditional human ratification is recorded in the ADR metadata and decision matrix.

- ADR-001: Harness as Sole Control Plane
  - Preserve existing P6.11 Loop/Worker work.
  - Prepare a CR-01 boundary-remediation plan, execution/state-transition contract tests, staged validation baseline and incremental migration path before any runtime request.
  - This decision does not authorize Phase B or runtime changes.
- ADR-004: Context Service versus Knowledge Memory Boundary
  - Do not delete, rename or directly restructure P6.7 `memory`.
  - Prepare P5.6/P6.7 compatibility contract, inventory, regression tests and compatibility alias/transition adapter before any future boundary work.
  - Keep Phase-2 Memory provider-neutral; do not install GBrain or introduce GBrain-specific domain dependencies.

## Proposed and deferred ADRs

- Remaining `PROPOSED`: ADR-005, ADR-006, ADR-007, ADR-009, ADR-010, ADR-011, ADR-012, ADR-014 and ADR-015.
- ADR-012 is explicitly deferred to Phase 2.

## Blocking conflicts

CR-01 through CR-05 remain unresolved. They are blocking for their applicable Phase-B-or-later implementation activities and do not authorize a workaround.

## P6.11 protection statement

The P6.11 worktree, runtime code, tests, migrations, staged validation baseline and current branch state remain protected. This decision changes no P6.11 implementation artifact and creates no release tag, staged snapshot or checkpoint commit.

## Next permitted action

- Preserve P6.11.
- Baseline P6.11 regression evidence without modifying tests.
- Collect CR-01 and CR-02 evidence.
- Prepare a Phase A3 evidence plan.

## Next forbidden action

- Phase B implementation, runtime refactor, migration or schema/database change.
- MCP implementation, GBrain integration, Provider Router implementation or provider automation.
- P5.6/P6.7 module deletion/rename, Loop/Worker restructuring or commit creation.
