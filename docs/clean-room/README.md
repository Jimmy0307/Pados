# Clean-room reimplementation: process

**Scope.** Eight files are identified by the consolidation provenance record as adapted from internal repositories:

- `src/pados/execution/{context,handoff,receipts}.py`
- `src/pados/product_loop/state.py`
- `src/pados/knowledge/{identity,adapters}.py`
- `web/src/{resourceCache,useResource}.ts`

**Decision:** `CLEAN_ROOM_REIMPLEMENT_REQUIRED`.

## Roles

| Role | Who | May see | Output |
|---|---|---|---|
| Specification author ("dirty room") | The primary agent of this release run. It had seen the old implementations earlier in the session | Old implementations, callers, tests, public docs | Behavioural contracts (this folder): signatures, rules, error codes, serialization formats. No implementation expression |
| Implementer ("clean room") | A **fresh subagent with no prior conversation context** | Only the quarantined export (below) | The 8 replacement files |
| Auditor | The primary agent | Everything, including donors (read-only) | Similarity audit report |

## Quarantine (technical)

The implementer works in a plain directory export, **not a git worktree**, so it has no repository history. The export excludes:

- `.git`, all `__pycache__`/`*.pyc`, `web/dist`, `build`;
- `runtime/`, `work/`, `outputs/`, `wiki/`, `.migration/`, `docs/migration/`;
- the eight old files, which are deleted from the export.

It contains only these specifications, the rest of the PADOS source (the callers and integration points), tests and public docs. The implementer is instructed not to read any path outside the export. That rule is procedural: the operating system does not enforce it, and this limitation is recorded in the reimplementation report.

## Permitted specification sources

- Public PADOS architecture documents; accepted interface definitions; type signatures needed for compatibility.
- Externally observable behaviour; public-safe tests; API and serialization schemas.
- Product requirements and generic industry concepts.

**Persisted data formats are contracts, not implementation.** These include the receipt payload, hash rules, and marker strings stored in objects; they must stay byte-compatible so that existing receipts remain verifiable.
