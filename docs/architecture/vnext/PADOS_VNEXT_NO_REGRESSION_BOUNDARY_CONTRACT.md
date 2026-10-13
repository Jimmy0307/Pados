# PADOS vNext No-regression Boundary Contract

Status: `PROPOSED`. This governs future implementation phases.

## Protected behavior

1. PostgreSQL remains the operational System of Record.
2. Existing project-root validation, command allowlists, secret screening and append-only event protections may not be weakened.
3. Existing task, loop, worker and human-gate transitions remain fail-closed until a replacement is characterization-tested and approved.
4. Mock providers remain test-only and cannot become a production fallback.
5. Obsidian remains a projection/control surface; it does not directly mutate operational records.
6. Existing releases retain manifest, hash and secret-scan safeguards.

## Change gates

Before a phase changes a boundary, it must have: an approved ADR, an explicit allowlist, characterization tests for preserved behavior, migration/reconciliation/rollback design where persistence is involved, security tests for project isolation and secret handling, and an evidence report with commands, exit codes and log paths.

## Explicitly prohibited without separate approval

- Runtime-code refactor disguised as documentation work.
- Schema or migration changes.
- Data backfill, provider credential use, GBrain installation, MCP server creation or external calls.
- Direct writes to `main`, automatic merge, push, reset or history rewrite.
- Replacing legacy behavior before the associated vNext contract and rollback evidence are accepted.
