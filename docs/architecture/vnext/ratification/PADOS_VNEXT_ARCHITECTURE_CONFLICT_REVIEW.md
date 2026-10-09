# PADOS vNext Architecture Conflict Review

Status: `RECOMMENDATION ONLY`.

| Conflict ID | Affected ADR | Current behavior | Target behavior | Risk | Evidence | Suggested resolution | Blocking |
| --- | --- | --- | --- | --- | --- | --- | --- |
| CR-01 | 001, 005 | Loop and worker services can orchestrate routing and execution adjacent to state changes | Harness owns commands; adapters return observations only | High | `loop/service.py`, `worker/service.py`, `agents/loop.py` imports | Characterize legacy flows, introduce command facade, then remove direct mutation paths | Blocks Phase B execution refactor |
| CR-02 | 004, 012 | `memory` combines source, packet and knowledge-like records | Context Service has packet authority; Memory is Phase 2 provider | High | P6.7 migration and `memory/*` inventory | Preserve tables/functions, add compatibility mapping before ownership changes | Blocks Context physical split only |
| CR-03 | 002, 010 | Artifact/test/evidence representations are fragmented | One logical immutable evidence relation with storage metadata | High | migrations `0001`, `0003`, `0005`; release package | Define logical contract and characterize existing hashes before consolidation | Blocks evidence schema consolidation |
| CR-04 | 006, 011 | Provider adapters exist without a complete billing/automation registry | Execution mode and token source are explicit | High | provider factory and runtime contract | Gather provider terms and telemetry evidence; do not enable automation | Blocks provider automation and cost routing |
| CR-05 | 007 | No MCP boundary or command mapping exists | Separate inbound/outbound gateways with scopes | High | no MCP modules identified | Threat model and application-command design before any server/client | Blocks MCP implementation |
| CR-06 | 009 | Multiple append-only streams and mutable current-state tables are separate | Event correlation plus current-state projections | Medium | append-only triggers in migrations `0003`, `0005`, `0006`, `0007` | Retain streams; define correlation envelope without replaying history | Does not block Phase A |
| CR-07 | 013 | Obsidian control surface exists alongside SQL control services | Obsidian is explicit projection and command client | Medium | `control/projections.py`, P6.8 schema | Add source-of-truth and drift contract; do not change vault behavior now | Blocks Obsidian expansion only |
| CR-08 | 014 | Runtime contract covers native/WSL/Docker; cloud/hybrid is target-only | Capability-based multi-mode deployment | Medium | `PADOS_RUNTIME_CONTRACT.md` | Accept architecture conditionally; defer remote configuration | Does not block local boundary work |
| CR-09 | 015 | Approvals and human gates are separated by aggregate | Unified policy-scoped approvals later | High | control approvals and P6.11 human gates | Preserve both as compatibility surfaces; no premature unification | Blocks approval consolidation |

## No-regression conclusion

No conflict justifies changing P6.11, runtime code, migrations or tests during A1. CR-01 through CR-05 are Phase-B-or-later blockers, not reasons to bypass the governance sequence.
