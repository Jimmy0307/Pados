# PADOS Studio + JPC Enterprise: product split and backend scorecard

**Status: provisional architecture evaluation, not an accepted implementation decision.** Scores are based on the desired separation and PADOS's current repository architecture. No JPC repository, source, data, credentials, configuration, or internal endpoint was inspected.

## Product boundary

### PADOS Studio — personal / independent studio

- Personal or small-studio work, projects, memory, and user-selected resources.
- PADOS owns its task, policy, execution receipt, verification, approval, state, and CAS path.
- Personal Home Base deployment and data remain under the user's control.

### JPC Enterprise Workbench — company-grade product surface

- Company users, roles, business projects, data classification, retention, audit, and approved enterprise integrations.
- JPC remains the authority for company-owned business records and policy unless the company explicitly chooses another owner.
- This document treats the JPC backend as an opaque candidate; its actual technology and controls are unverified.

### Shared only by explicit contract

- Generic interaction patterns/design tokens and versioned, data-minimized API/capability contracts may be reused after review.
- Do not share user sessions, credentials, databases, artifact roots, company source/data, internal endpoints, or proprietary UI assets between the products.
- An integration carries only authorized input, bounded output, provenance, and evidence. Each product commits its own semantic state through its own authority path.

## Backend topology options

Scores use 1–5 (5 is best). “Integration simplicity” and “operations/cost” score higher when burden/cost is lower.

| Criterion | Weight |
|---|---:|
| Authority and data isolation | 25% |
| Company data / IP boundary | 20% |
| Generic core reuse | 15% |
| Integration simplicity | 15% |
| Operations / cost | 10% |
| Reversibility / portability | 15% |

| Option | Authority / isolation | Data / IP | Reuse | Integration | Ops / cost | Reversibility | Weighted score |
|---|---:|---:|---:|---:|---:|---:|---:|
| A. One PADOS backend deployment and shared datastore for both frontends | 1 | 1 | 5 | 5 | 5 | 2 | **2.75 / 5** |
| B. Fully independent PADOS and JPC backend stacks; explicit API bridge only | 5 | 5 | 2 | 2 | 2 | 5 | **3.80 / 5** |
| C. Reuse generic PADOS backend code, deploy isolated PADOS instances; JPC retains its own backend and authority behind reviewed adapters | 5 | 5 | 4 | 4 | 3 | 5 | **4.50 / 5** |
| D. JPC backend remains the only enterprise backend; PADOS stays a personal product and has no enterprise PADOS deployment | 5 | 5 | 2 | 3 | 4 | 4 | **4.00 / 5** |

Calculation: sum of `(criterion score × weight) / 100`.

## Provisional recommendation

**For the product split currently described (PADOS for a personal/small studio; JPC for company-grade use), use Option D as the provisional product baseline.** Keep PADOS Studio and JPC Enterprise Workbench on their own backend and authority paths. PADOS remains personal-only; JPC owns company business records and their semantic commit path. This gives the clearest product and data boundary without assuming that PADOS must enter enterprise workflows.

**Option C is a conditional future topology, not the current selection.** Consider it only if there is a confirmed requirement for PADOS to orchestrate approved enterprise work. In that case, reuse generic PADOS backend code in a separately deployed enterprise PADOS instance with separate identity, policy configuration, database, object roots, secret store, network boundary, and release controls. JPC remains the system of record for company business data and exposes only reviewed, versioned APIs/capabilities. PADOS receipts are evidence for PADOS work; they do not directly commit company records. C scores highest in this scorecard because it assumes this added enterprise orchestration/reuse requirement; its score does not override the current product boundary.

Option A is not recommended: two visual skins over one backend/datastore blur authority and data boundaries. Option B is a strong isolation alternative if PADOS and JPC must use wholly independent backend implementations, but it costs more to operate and integrate than D under the current product split.

Do not choose Option A just to reduce integration work. A shared backend with two visual skins blurs authority and data boundaries. The separation choices in multi-tenant architecture are a continuum: bridge designs can share selected tiers while isolating business logic and storage; dedicated deployment stamps improve isolation but add operating cost. These patterns support the scorecard's trade-off framing, not a claim about JPC's actual backend. [AWS SaaS Lens: bridge model](https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/bridge-model.html), [Azure Architecture Center: tenancy models](https://learn.microsoft.com/en-us/azure/architecture/guide/multitenant/considerations/tenancy-models), [Azure deployment stamps](https://learn.microsoft.com/en-us/azure/architecture/patterns/deployment-stamp).

## Technology selection is still open

This scores **deployment/authority topology**, not framework vendors. The PADOS repository currently uses Python/FastAPI, PostgreSQL, SQLAlchemy/Alembic, and a static React/Vite frontend. That makes the existing backend a concrete candidate for PADOS instances; it does not establish that it is the right implementation for JPC. Before comparing frameworks or databases, use an approved, sanitized JPC inventory covering API/auth, deployment model, data ownership, tenancy, required integrations, compliance/retention, and operating constraints. Do not inspect private repositories or copy private implementation details into PADOS.
