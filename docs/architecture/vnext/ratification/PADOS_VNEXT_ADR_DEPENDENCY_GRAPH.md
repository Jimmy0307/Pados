# PADOS vNext ADR Dependency Graph

Status: `RECOMMENDATION ONLY`.

```mermaid
flowchart TB
    A1["ADR-001 Harness"] --> A3["ADR-003 Modular Monolith"]
    A1 --> A4["ADR-004 Context Boundary"]
    A1 --> A5["ADR-005 Adapter Execution"]
    A1 --> A7["ADR-007 MCP Boundary"]
    A1 --> A9["ADR-009 Append-only Audit"]
    A1 --> A15["ADR-015 Human Gates"]
    A2["ADR-002 PostgreSQL SoR"] --> A4
    A2 --> A9
    A2 --> A10["ADR-010 Evidence Immutability"]
    A2 --> A13["ADR-013 Obsidian Projection"]
    A8["ADR-008 Project Isolation"] --> A4
    A8 --> A5
    A8 --> A7
    A8 --> A12["ADR-012 Memory Compatibility"]
    A5 --> A6["ADR-006 Subscription vs API"]
    A5 --> A11["ADR-011 Token Governance"]
    A6 --> A11
    A4 --> A12
    A9 --> A10
    A15 --> A5
    A15 --> A7
    A14["ADR-014 Deployment"] --> A5
    A14 --> A8
```

## Interpretation

- **Foundational**: ADR-001, ADR-002, ADR-003, ADR-004, ADR-008 and ADR-013.
- **Execution dependent**: ADR-005 depends on Harness and isolation; ADR-006 and ADR-011 require provider evidence before operational use.
- **Phase 2 dependent**: ADR-012 depends on Context boundary and project isolation; it must not block Phase A/B.
- **P6.11 protective dependencies**: ADR-001, ADR-004, ADR-008, ADR-009, ADR-010 and ADR-015 require preservation of current P6.11 transitions, events, evidence and human gates.
- **Evidence-sensitive**: ADR-006 needs subscription/provider automation evidence; ADR-011 needs usage, billing and credential-governance evidence; ADR-007 needs threat-model evidence before implementation.
