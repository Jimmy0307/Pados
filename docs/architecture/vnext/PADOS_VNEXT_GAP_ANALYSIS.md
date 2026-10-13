# PADOS vNext Gap Analysis

Status: `PROPOSED`. This is a logical comparison; it makes no claim that a migration is ready.

| Target concern | P6.x evidence | Assessment | Required architectural action |
| --- | --- | --- | --- |
| Harness sole control plane | P6.x has state machines, gates and services, but execution paths are distributed | PARTIAL MATCH | Establish one application-command boundary for state changes and dispatch decisions |
| PostgreSQL system of record | Constitution and SQL migrations designate PostgreSQL for operational state | MATCH | Preserve; prohibit Memory, Obsidian, MCP and provider-owned status as authoritative |
| Context Service separate from Memory | `memory/*` stores sources, retrieval, packet and memory records together | CONFLICT | Reframe P5.6 as Context Service; reserve Memory Provider contract for Phase 2 |
| Retrieval separate from execution | worker and loop services directly build or request context | PARTIAL MATCH | Context Builder returns immutable pack; execution only receives pack reference and content |
| Agent cannot mutate task state | worker/loop services participate in workflow transitions | PARTIAL MATCH | Agent adapters report outcomes only; Harness applies state transitions |
| Provider independent of domain | `LLMProvider` protocol exists, but factory/core router use provider-specific defaults | PARTIAL MATCH | Create provider registry, capability and execution-mode contracts |
| MCP distinct from Harness | No MCP boundary exists | MISSING | Add outbound gateway and inbound server as separately governed adapters |
| Filesystem non-authoritative | Hash pointers and root checks exist; files are also read directly by execution paths | PARTIAL MATCH | Treat filesystem/object storage as artifact storage with PostgreSQL metadata authority |
| Artifact / validation / evidence separation | Multiple tables and services capture pieces of evidence | PARTIAL MATCH | Define immutable artifact, validation run and evidence relations |
| Task transition governance | Core and loop transitions are bounded | PARTIAL MATCH | Normalize a single transition authority per aggregate and forbid adapter transition calls |
| Immutable execution history | Several append-only event triggers exist | PARTIAL MATCH | Require event stream for every execution and transactional outbox for integrations |
| Project boundary and root path | Indexer and command service validate roots | MATCH | Generalize to project/workspace/source/storage scopes |
| Mock isolation | Factory requires explicit mock allowance, but historical default remains visible | PARTIAL MATCH | Restrict mock to test profiles; prevent production route eligibility |
| Approval and release gates | Maker-checker and project human gates exist | PARTIAL MATCH | Make approvals policy-scoped, expiring, immutable and mandatory for high-risk actions |
| Subscription vs API execution | Existing adapters largely assume controlled CLI or local provider behavior | MISSING | Model interactive subscriptions separately from headless metered API execution |
| Token / cost governance | Resource limits exist; provider cost/quota model does not | MISSING | Add logical token sources, quota/cost routing and usage evidence |
| Phase-2 memory compatibility | No provider-neutral Memory contract exists | DEFERRED TO PHASE 2 | Define capability matrix only; do not introduce GBrain dependency |

## Priority corrections

Critical:

1. Prevent any future adapter, MCP tool or Memory provider from directly changing workflow state.
2. Preserve PostgreSQL as the only authority for projects, tasks, executions, approvals, releases and audit.
3. Separate subscription-supervised interaction from headless API automation.

High:

1. Split Context Service responsibilities from Knowledge Memory semantics.
2. Replace provider factory assumptions with registry-driven policy routing.
3. Normalize immutable artifact, validation and evidence links.
4. Introduce a formal MCP inbound/outbound boundary.

Medium:

1. Consolidate duplicate project and approval representations after characterization evidence exists.
2. Replace direct API/CLI-to-domain coupling with application command handlers.
3. Harmonize legacy documentation status before declaring vNext implementation work complete.
