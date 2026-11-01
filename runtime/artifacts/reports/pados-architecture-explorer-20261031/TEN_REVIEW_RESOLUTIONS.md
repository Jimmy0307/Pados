# PADOS core capabilities and ten architecture review items

## Evidence snapshot

- Repository: `Jimmy0307/Pados`
- Canonical main: `6f5eecb6a98cc61edabdbba7a2a653ee0aa2d416` (PR #18 merged)
- Audited checkout: `feat/capability-expansion-v1` at `4fd1a249b195f0c4a2cb37a6c7d826aedb5d3db7`
- Source tree SHA-1: `c73bcc2c6c8b548d5858951dc43d90b623ac8697` (identical at audited checkout and canonical main)
- This is an architecture/source audit. No application source was edited and no tests were run in this audit.

## What PADOS Core does

**PADOS Core turns a person's original intent into governed, evidence-backed, reviewable work across replaceable resources, then commits an accepted outcome through PADOS-owned authority.** The differentiating core is control of the task lifecycle and its evidence, not any particular model, domain pack, or UI.

1. **Preserve intent and task identity.** Capture the authenticated request, project, scope, classification, and goal relations in PADOS-owned contracts (`TaskEnvelope` and related protocols).
2. **Govern capability and resource use.** Check policy, grants, scope, classification, feasibility, approval requirements, and provider/runtime identity before dispatch.
3. **Coordinate work.** Use the existing scheduler for queue order and the existing router for resource selection; track partial coverage and authorized residual work.
4. **Execute through replaceable resources.** Invoke workers, local/cloud providers, compute nodes, tools, deterministic algorithms, and packs through bounded adapters and execution contracts.
5. **Retain evidence and verify.** Bind outputs to execution receipts, artifact digests, provenance, and runtime/device/config evidence; verify coverage and output contracts.
6. **Accept outcomes under governance.** Keep results as `ResultCandidate` until required verification and human/governed approval succeed; commit through the existing CAS and audit path.
7. **Carry context and improve under gates.** Provide project context, memory, retrieval, discovery, and outcome-learning evidence. These may inform future work but cannot authorize, dispatch, or promote themselves.

**Extension capabilities** are task-specific skills such as OCR, retrieval, DRSA/FCM/MCDM, structured problem solving/8D, research execution, MCP tools, and local inference. They can expand what tasks PADOS can perform; they do not own the task, policy, scheduler, router, approval, verification, state, or acceptance authority.

## Ten-item resolution ledger

| # | Review item | Finding and disposition | Status |
|---|---|---|---|
| 1 | Product Loop vs TaskLoop / WorkProtocol | They are two distinct application flows. `product_loop` runs its `GoalRequest` / `WorkContract` direct-maker workflow. `loop` runs persisted TaskLoop iterations using `TaskEnvelope`; `work_protocol` supplies its task, coverage, composition, residual, verification, and lineage contracts. Both are mounted/used by Home Base and both call the same `StateProofService.commit_result` implementation. They are not one unified task schema, so the product must describe them as separate flows until a separately authorized convergence decision. | **CLARIFIED; convergence is a product decision** |
| 2 | `pados.graph` vs Work Graph | `pados.graph` exposes local cognitive/workspace graph APIs for nodes, intent, URL ingestion, and synthesis. `work_protocol.graph` derives a rebuildable task/result/acceptance lineage projection. Similar naming does not imply shared storage or authority. The diagram now labels them separately. | **RESOLVED BY SOURCE** |
| 3 | Semantic CAS path | Both TaskLoop acceptance and ProductLoop's state gateway call `StateProofService.commit_result`, which delegates to the single repository CAS implementation. ProductLoop also rejects using the same product and operational database names. The CAS implementation is shared; configured database instances/lanes may differ. | **RESOLVED BY SOURCE; deployment wiring remains configuration-specific** |
| 4 | Capability Pack runtime enforcement | V1 validates the manifest and enforces the pack classification ceiling at request time. The spec explicitly says timeout, egress, allowed roots, and SecretRefs are declarations/consistency checks; there is no general isolation runner enforcing those fields at runtime. This is a stated residual, not a solved enforcement feature. Do not register unreviewed in-process code; non-in-tree code must use an isolated runtime declaration. | **DOCUMENTED RESIDUAL; requires product risk acceptance or a separately scoped runtime project** |
| 5 | REA / OpenMAIC live runtime | Their adapters and failure contracts are implemented, but project docs explicitly retain `NOT_VALIDATED` for real runtimes. No fake/CI result can close this. | **EXTERNAL VALIDATION OPEN** |
| 6 | Windows / VMware Linux evidence | For exact HEAD `4fd1a24`, Linux evidence records host `pados-dev-linux`, `virt=vmware`, and a full-suite rerun with exit 0. Windows D1/D2/D4 and static gates pass, but Windows `pack_focused` and full pytest exit 1. The operator decision code explicitly refuses Windows identity issuance as `OPERATOR_PLATFORM_UNSUPPORTED`; the full suite also records PostgreSQL password-auth failures. Windows product acceptance is therefore not closed. | **VMWARE VERIFIED; WINDOWS BLOCKED** |
| 7 | Work Map Pack management | Pack V1 explicitly lists “no UI” as a non-goal. Missing Pack management controls in Work Map are not a V1 implementation defect. If required, treat it as a later UI requirement; preserve install/registration/authorization as separate states. | **OUT OF V1 SCOPE** |
| 8 | Dependency provenance / publication rights | Python and npm dependencies are inventoried and third-party notices/provenance documents exist. The release policy still says public publication needs copyright/CLA legal review and an explicit human publication decision. An inventory is not rights clearance. | **OWNER / LEGAL GATE OPEN** |
| 9 | Pack vs Core authority boundary | `tests/capability/test_pack_boundaries.py` checks that pack code cannot import/call scheduler, binding, semantic acceptance, or CAS authority paths. Linux pack-focused gate is recorded as exit 0 on this HEAD. This supports the stated boundary; it does not erase the separate Windows failures in item 6. | **BOUNDARY EVIDENCE PRESENT; LINUX GATE PASS** |
| 10 | Database/object-store topology | The repository defines distinct operational, corpus, and optional product/proof database names; ProductLoop requires proof DB and operational DB to differ. It also takes product workspace/object roots from configuration, and Compose declares a local PostgreSQL/pgvector development service. The logical architecture is documented. The actual active deployment's database names, migration heads, and object-root mappings require a redacted environment snapshot; secrets must not be copied into this report. | **LOGICAL MAP RESOLVED; INSTANCE MAP NEEDS REDACTED EVIDENCE** |

## Evidence paths

- `src/pados/api/app.py` — mounts the ProductLoop routes.
- `src/pados/loop/service.py` and `src/pados/loop/acceptance.py` — TaskEnvelope-backed TaskLoop and governed acceptance.
- `src/pados/product_loop/routes.py` and `src/pados/product_loop/state.py` — separate direct-maker workflow and shared proof service.
- `src/pados/graph/routes.py` vs `src/pados/work_protocol/graph.py` — distinct graph API and lineage projection.
- `src/pados/state_proof/service.py` and `src/pados/state_proof/repository.py` — shared CAS service/repository.
- `docs/superpowers/specs/2026-10-31-capability-pack-v1-design.md` — Pack runtime limits and V1 non-goals.
- `tests/capability/test_pack_boundaries.py` — pack/Core boundary regressions.
- `runtime/artifacts/reports/capability-expansion-20261031/linux-vm-gates-4fd1a24/summary.json` and `full_pytest_rerun.log` — exact-head VMware run and successful full-suite rerun.
- `runtime/artifacts/reports/capability-expansion-20261031/windows-gates-4fd1a24/gate-summary.json`, `pack_focused.log`, and `full_pytest.log` — exact-head Windows gate outcomes and residual failures.
- `docs/product/RELEASE_CHANNELS.md` and `THIRD_PARTY_NOTICES.md` — release/legal status and dependency notices.
- `src/pados/config.py`, `src/pados/product_loop/routes.py`, and `docker-compose.yml` — configured database/object-store names and development DB service.

## What remains before calling all ten closed

There are five explicit closure conditions: accept or fund enforcement for the Pack V1 declared-only limits; execute live REA/OpenMAIC validation; close Windows-native operator identity and rerun Windows full gates with the correct disposable DB; obtain owner/legal publication clearance; and record the active deployment's redacted database/object-store map. None can be truthfully marked closed from static architecture documentation alone.
