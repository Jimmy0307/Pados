# ADR-008: Project Isolation and Repository Boundary

Status: `ACCEPTED`

Ratified On: `2026-07-13`

Decision Authority: `Human`

Implementation Authorization: `NOT GRANTED`

## Context
P6.x validates registered project roots for indexing and registered commands.

## Decision
Every operation carries project scope; roots, workspaces, sources, storage and tools are resolved inside explicit allowlists.

## Alternatives Considered
Process working-directory trust, unrestricted file paths or agent-selected roots.

## Consequences
Cross-project retrieval, artifact and execution access fail closed.

## Risks
Legitimate multi-repository workflows require explicit workspace registration.

## Migration Impact
Extend existing root tests before generalizing scopes.

## Validation Criteria
Path escape, symlink, cross-project retrieval and mixed-workspace tests are mandatory.
