# PADOS open-core boundary V1 (draft: options only)

> **Decision recorded (owner decision OD-1..OD-4).** PADOS Core is licensed `AGPL-3.0-only`, with separate commercial licensing available from the copyright holder (dual licensing); separable SDKs/schemas may be `Apache-2.0` only under `sdk/`; contributions will require a CLA. See `LICENSING.md`, `COMMERCIAL.md` and `docs/legal/CLA_RECOMMENDATION.md`. The text below is kept as the historical decision record.

**No software license is chosen in this document** (updated for Local Compute Node V1; still options only). It classifies what exists today and lists options for a later human decision.

## Open-core candidates (implemented)

| Feature | Implementation |
|---|---|
| Project / Context / Decision contracts | `pados.execution.context`, `pados.project_goal`, `pados.memory` decision records |
| Receipt contracts | `pados.execution.receipts` (ExecutionReceipt), `pados.state_proof` (ResultReceipt) |
| ScopeVersion / CAS | `pados.state_proof` (proof lane) |
| Capability SDK | `pados.enterprise.manifest`, `registry`, `authorization` (adapter over the PADOS grant) |
| Provider SDK | `pados.worker` (Worker Router, `WorkerSpec`), `pados.enterprise.compute.ProviderCapabilityProfile` |
| Source identity | `pados.knowledge.identity`, `pados.enterprise.lineage` |
| Basic enterprise adapters | Text/PDF extraction, in-process exact/vector retrieval, no-op/lexical/HTTP rerankers, budgets, SecretRef boundary, content-free observability |
| Local Compute Node protocol | `pados.enterprise.compute.ComputeNode`, `LocalInferenceProvider` (contract only) |
| Basic Mission Control | `pados.operations`, web Control Center (including the enterprise panel) |

## Local Compute Node: open-core candidates (implemented in Local Compute Node V1)

| Feature | Implementation |
|---|---|
| Node protocol | `pados.compute-node.protocol.v1` (docs/local-compute/NODE_PROTOCOL.md) |
| Compute-node daemon | `pados node serve` (`pados.compute_node.service`) |
| Capability and model manifest | `pados.compute_node.manifest` (NodeDescriptor, ModelManifest) over the Enterprise manifest and ComputeNode contracts |
| Provider SDK | `pados.worker` plus `ProviderCapabilityProfile`; `InferenceBackend` protocol |
| LocalInferenceProvider | `pados.compute_node.provider`, routing through the existing Worker Router |
| Basic adapters | OpenAI-compatible (generic), Ollama, deterministic; MLX via the OpenAI-compatible contract |
| Health and doctor integration | `pados doctor` `compute_nodes` section, `pados node probe` |
| Basic Mission Control node view | `compute_nodes` projection plus `ComputeNodesPanel` |

## Local Compute Node: commercial candidates (future, not implemented)

- Enterprise fleet management (many nodes, inventory, lifecycle).
- High availability and failover.
- Remote administration.
- Centralized node policy.
- Compliance reporting over node receipts.
- Advanced observability (OTel export of `pados.*` node events).
- Deployment automation (node install and upgrade).

## Enterprise commercial candidates (not implemented; boundary reserved)

| Feature | Notes |
|---|---|
| Enterprise SSO | An identity adapter would produce `AccessContext`. PADOS has none today |
| Organization-wide RBAC | Must stay an adapter into the PADOS grant (no second ACL store) |
| Advanced policy enforcement | e.g. egress/classification policy engines beyond `allow_remote_egress` |
| Compliance reporting | Built on the existing immutable semantic audit, not a new audit authority |
| Fleet management | Many `ComputeNode`s; would need a governed design (no scheduler exists today) |
| Advanced observability | OTel exporter for the `pados.*` event attributes |
| Deployment automation | — |
| Enterprise connectors | SharePoint/Confluence/ERP adapters producing `EnterpriseSource` |
| Support/SLA | — |

## Licensing options (for decision, not chosen)

| Option | Shape | Trade-offs |
|---|---|---|
| A. Permissive core (MIT/Apache-2.0) plus proprietary enterprise modules | Separate packages or repos | Maximum adoption; competitors can reuse the core freely |
| B. Copyleft core (AGPL-3.0) plus commercial license | Dual licensing | Protects hosted use; some enterprises avoid AGPL |
| C. Source-available core (e.g. BSL/FSL with a change date) | Time-delayed open source | Protects commercial use; not OSI-approved |
| D. Modified Apache with usage restrictions (as Dify does; see the gap analysis) | Custom terms | Precedent exists; custom licenses add legal review cost |

**Prerequisites before any choice:**

1. Confirm ownership of all code in the tree.
2. Resolve donor-derived code (THIRD_PARTY_PROVENANCE.md §2): confirm publication rights or reimplement clean-room.
3. Split packages so that `pados.enterprise` commercial modules can be separated.
4. Obtain legal review.


## Productization V1: final technical boundary (still no license chosen)

**Principle.** The open version must remain useful on its own: a single user can run the full governed chain locally, including local models. Nothing is artificially crippled, and there are no licensing checks.

| Open (candidate) | Enterprise / commercial (candidate) |
|---|---|
| Core contracts: Project, Context, Decision, Goal, Plan, Gap | Organization SSO, identity federation |
| ScopeVersion/CAS, receipt model, immutable semantic audit | Centralized RBAC and policy administration |
| Basic Memory Authority (operational) | Fleet management for compute nodes (inventory, lifecycle, HA) |
| Capability SDK (`pados.enterprise` manifests, classification, authorization adapter, retrieval, rerank, lineage, budgets, SecretRef, observability) | Compliance packs; advanced audit reporting |
| Provider SDK (Worker Router, provider profiles) | HA deployment |
| Source identity, classification | Centralized observability (OTel pipelines, dashboards) |
| Local Compute Node protocol, node daemon, LocalInferenceProvider | Enterprise connector packs (document stores, ERP, ticketing) |
| Ollama, OpenAI-compatible and llama.cpp adapters; deterministic backend; MLX contract | Organization management; support/SLA |
| Basic Mission Control and Decision Inbox, doctor, CLI | Managed deployment tooling; hardened (asymmetric) node identity |
| Local single-user deployment and TRUSTED_LAN nodes | |

Implementation remains one codebase; any future split is a packaging boundary, not a behavioural fork.
