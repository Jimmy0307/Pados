# PADOS vNext P5.6 Compatibility Plan

Status: `PROPOSED`; no P5.6/P6.x implementation change is authorized.

## Reframing

The P6.7 `memory` implementation becomes the compatibility source for **Context Service Foundation**, not proof that PADOS has adopted a Knowledge Memory engine. The reusable functions are project registration, source registration, source-boundary checks, freshness, retrieval, ranking, token budgeting, packet assembly, secret screening, packet hashing and lineage.

## Compatibility mapping

| Existing concept | vNext name | Compatibility treatment |
| --- | --- | --- |
| `context_project_registry` | Project/Workspace registration reference | Retain data as legacy source; reconcile ownership only after characterization tests |
| `context_sources` | Source Registry | Reuse with explicit source version, ACL, trust and lineage extensions |
| `memory_records` / relationships / decisions | Legacy knowledge-like records | Freeze expansion; classify for future Memory Provider migration or Context metadata retention |
| `context_packets` / items | Bounded Context Pack | Preserve hash and audit semantics; make immutable contract explicit |
| retrieval/token budget | Retrieval Provider and Context Ranking | Reuse deterministic behavior behind a port |
| run events | Execution/Audit events | Preserve append-only history; normalize correlation later |

## Required future safeguards

1. No direct agent execution coupling inside Context Service.
2. No GBrain-specific schema, SDK or provider assumption in the Harness.
3. Context Pack must record source lineage, trust/freshness ranking, exclusions and policy version.
4. Existing P6.x behavior must receive characterization tests before any rename, split or table ownership change.
5. Any later physical migration must have rollback, data reconciliation and cross-project leakage validation.
