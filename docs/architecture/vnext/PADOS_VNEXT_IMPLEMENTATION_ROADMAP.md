# PADOS vNext Implementation Roadmap

Status: `PROPOSED`. No phase is authorized by this roadmap alone.

| Phase | Goal | Entry criteria | Exit criteria | Rollback boundary |
| --- | --- | --- | --- | --- |
| A: Architecture Ratification | Approve vNext invariants and ADRs | Review complete | ADR statuses explicitly accepted by human decision | No code change |
| B: Critical Boundary Fixes | Protect control-plane, roots, mocks, transitions | Characterization baseline accepted | No adapter path mutates state; no mock production path; root controls proven | Isolated worktree and reversible code change only |
| C: Execution and Provider Contracts | Create provider/agent and execution-mode abstractions | B accepted | Subscription/API/local modes distinguished; routing decision auditable | Adapter feature flag and old route retained |
| D: Context Service P5.6 Realignment | Reframe P6.7 as Context Service | C accepted | Source registry, retrieval port, ranking and immutable packs pass compatibility tests | Legacy reads preserved until reconciliation accepted |
| E: Token and Cost Governance | Govern quota, rate limits, budgets and fallback | C accepted | Token source contract and usage evidence pass policy tests | Routing remains fail-closed if registry unavailable |
| F: MCP Gateway | Add inbound/outbound integration boundary | B and C accepted | Scope, authentication, audit and failure tests pass; no direct DB access | Gateway disabled without changing Harness state |
| G: Human Workspace / Obsidian | Projection and human commands | D accepted | No direct state writes; projection drift and authorization tests pass | Disable projection adapter only |
| H: Knowledge Memory / GBrain Phase 2 | Introduce provider-neutral memory capability | D and provider review accepted | Provider-neutral contract, isolation, export and lineage tests pass | Detach provider; retain operational metadata and packs |

## Common phase evidence

Every implementation phase requires a dedicated non-main worktree/branch, explicit allowlist, test plan, security plan, rollback plan, acceptance report under `runtime/artifacts/reports/`, and an independent review before release. No phase may use P6.11 unfinished changes as implicit authorization or baseline without a separate human decision.
