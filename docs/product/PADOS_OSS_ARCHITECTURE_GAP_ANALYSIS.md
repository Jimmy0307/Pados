# PADOS OSS architecture gap analysis

## Method

- **Date:** read-only public-source research on 2026-09-30. The research agent used web fetches, the GitHub API and raw files. Load-bearing items were re-checked against raw text: the Dify hash check and license, the OpenSelf README, and the GitHub license metadata.
- **"NFS":** not found in the sources inspected. It means *not verified*, not "absent".
- **AnythingLLM:** docs.anythingllm.com returned HTTP 403, so cells marked *(snippet)* rely on search-engine excerpts of those pages.
- **AAiOS:** no primary source could be found. The closest names are the unrelated "AIOS" projects (e.g. arXiv 2403.16971). AAiOS is **not evaluated**.
- **Scope:** architecture, not marketing. Competitor claims come only from the cited sources.

## Projects and licenses

| Project | Primary source | License (as published) |
|---|---|---|
| OpenClaw | https://github.com/openclaw/openclaw | MIT (https://raw.githubusercontent.com/openclaw/openclaw/main/LICENSE) |
| OpenSelf | https://github.com/Open-Self/Open-Self | MIT (README badge, GitHub API). Early-stage; the description ("AI clone") and the README ("Personal Context OS") differ |
| Letta | https://github.com/letta-ai/letta, https://github.com/letta-ai/letta-code, https://docs.letta.com | Apache-2.0. The README says current development moved to letta-code; some docs may describe the retired V1 server |
| LocalAI | https://github.com/mudler/LocalAI | MIT |
| AnythingLLM | https://github.com/Mintplex-Labs/anything-llm | MIT |
| Dify | https://github.com/langgenius/dify | "Dify Open Source License" (modified Apache-2.0: multi-tenant service and logo-removal restrictions) |

## Comparison

**PADOS column source.** PADOS facts come from this repository:

- `pados.state_proof`
- `pados.execution`
- `pados.memory` (operational memory authority `20260930_0014`)
- `pados.worker`
- `pados.enterprise`

| Dimension | PADOS | OpenClaw | OpenSelf | Letta | LocalAI | AnythingLLM | Dify |
|---|---|---|---|---|---|---|---|
| Center of authority | PADOS DB lanes: operational plus separate proof lane (ScopeVersion/CAS, receipts, audit) | Gateway is "the local control plane"; "All session state is owned by the gateway" ([session](https://docs.openclaw.ai/concepts/session)) | Owner-approved local SQLite/FTS5 vault ([README](https://github.com/Open-Self/Open-Self)) | Agent state in DB (V1 docs); Letta Code MemFS tracked in git ([memory blocks](https://docs.letta.com/guides/agents/memory-blocks)) | Per-model YAML config; inference engine ([config](https://localai.io/docs/advanced/model-configuration/)) | App DB (SQLite/Prisma) plus vector DB (LanceDB default) ([README](https://github.com/Mintplex-Labs/anything-llm)) | App/workflow graph in the platform DB ([repo](https://github.com/langgenius/dify)) |
| Project state | Registered projects, goals, gaps, decisions, ScopeVersion per project | Per-agent SQLite sessions plus workspace folder | Single vault with typed memories | Stateful agents | Users in SQLite/Postgres; agent state on disk ([auth](https://localai.io/docs/features/authentication/)) | Workspaces as isolated containers | Workspace = tenant with apps and knowledge bases |
| Semantic truth | Only validated ResultReceipt plus expected-ScopeVersion CAS advances state; `MODEL OUTPUT != TRUTH` | NFS | Agents "can only *propose* memories"; owner approval required | NFS (agent edits its own memory) | NFS | NFS | Optional Human Input node ([human input](https://docs.dify.ai/en/use-dify/nodes/human-input)) |
| Memory authority | Operational `memory_authority_events` (append-only), promotion with provenance | Markdown files the agent writes via tools ([memory](https://docs.openclaw.ai/concepts/memory)) | Owner writes; agents propose via inbox; per-agent grants | Agent and API edit blocks; `read_only` locks the agent | LocalRecall collections; write rules NFS | *(snippet)* Automatic memory extraction, can be disabled ([memories](https://docs.anythingllm.com/features/memories)) | NFS |
| Execution receipts | ExecutionReceipt bound to contract hash and artifacts; ResultReceipt in proof DB | Session transcripts; dedicated receipts NFS | Ed25519-signed context receipt; hash-chained MCP audit log | OTEL run traces ([trace](https://docs.letta.com/api/python/resources/runs/subresources/trace/methods/retrieve)) | `/api/traces` request traces ([4.8 notes](https://localai.io/blog/what-landed-in-localai-4-8/)) | *(snippet)* Event logs ([event logs](https://docs.anythingllm.com/features/event-logs)) | Per-run node-level logs ([logs](https://docs.dify.ai/en/use-dify/debug/history-and-logs)) |
| CAS / version protection | Expected-ScopeVersion CAS on every semantic commit | NFS | Version history and supersede lineage; write CAS NFS | "last write wins" for blocks (memory-blocks doc) | NFS | NFS | `unique_hash` check on draft workflow save only ([workflow_service.py](https://github.com/langgenius/dify/blob/main/api/services/workflow_service.py)) |
| Provider abstraction | Worker Router (Codex CLI, Claude Code, Ollama, deterministic) plus capability profiles | Hosted and local providers via plugins | Embedding providers only | OpenAI, Anthropic, and others | OpenAI/Anthropic-compatible APIs over many backends | 30+ providers | Many providers, OpenAI-compatible |
| Local compute | Ollama worker; `ComputeNode`/`LocalInferenceProvider` contract (implementation deferred) | Ollama; node-local inference on paired machines ([ollama](https://docs.openclaw.ai/providers/ollama)) | Local-first; Ollama embeddings | Ollama, LM Studio ([ollama](https://docs.letta.com/models/ollama)) | Core purpose: llama.cpp, vLLM, MLX; CUDA, ROCm, Metal, CPU | llama.cpp, Ollama, desktop app | Self-hosted models (Ollama not verified) |
| Enterprise governance | Project-bound grants, SecretRef, two-stage enterprise authorization, classification ceilings; no SSO | "not a hostile multi-tenant security boundary" ([security](https://docs.openclaw.ai/gateway/security)) | Single owner; bearer token | NFS | Admin/user roles, API keys, quotas, OIDC SSO (OSS) | *(snippet)* Admin/Manager/Default roles, simple SSO ([security](https://docs.anythingllm.com/features/security-and-access)) | Community workspace roles; SSO, workflow RBAC and SIEM audit are Enterprise-only ([enterprise](https://dify.ai/dify-enterprise)) |
| Evidence / provenance | Content-hash source identity, typed lineage graph, evidence packages with spans, versions and authorization record | NFS | Source-attributed memories cited by hash in receipts | NFS | NFS | "source citations" (README) | Citation & Attribution with segment and score ([citation](https://docs.dify.ai/en/guides/knowledge-base/retrieval-test-and-citation)) |
| MCP / tools | Skills/MCP grants via `authorize_execution_capabilities`; MCP_TOOL denied by default | MCP client and server; skills/plugins ([mcp](https://docs.openclaw.ai/tools/mcp)) | MCP server (stdio/HTTP) | Tools core; MCP implementation not verified | MCP per model or agent ([mcp](https://localai.io/docs/features/mcp/)) | MCP-compatible agent builder | Plugins, MCP, custom APIs |
| Human approval | Decision Inbox, human gates, approval refs bound into contract and audit | Exec approvals: allowlist plus optional user approval ([exec approvals](https://docs.openclaw.ai/tools/exec-approvals)) | Owner inbox approval | `requires_approval` tool pause ([HITL](https://docs.letta.com/guides/agents/human-in-the-loop)) | Terminal agent approval gate; platform agents NFS | *(snippet)* Tool approval card; auto-approves in non-interactive runs ([handler](https://docs.anythingllm.com/agent/custom/handler-js)) | Human Input node with timeout branch |
| Deployment model | Local-first: WSL/Windows native, PostgreSQL lanes, web UI | npm daemon, Docker, Nix; no hosted tier per README | npm, Docker image | Cloud, local CLI/desktop, self-hosted server | Docker, binaries, Kubernetes, distributed mode | Desktop, Docker, cloud templates | Cloud, self-hosted Compose, Enterprise |

## Gaps and positioning (PADOS view, evidence-bounded)

1. **Semantic acceptance.** Among the sources inspected, only OpenSelf (owner approval) and Dify (optional human node) show an explicit acceptance step. None shows a CAS-protected semantic commit comparable to PADOS ScopeVersion/CAS. Dify's hash check protects draft workflow saves only. This is PADOS's clearest differentiator.
2. **Receipts and provenance.** OpenSelf's signed receipts and hash-chained audit are the closest analogue. PADOS should consider signed (not only hashed) receipts in a later phase; this is not implemented.
3. **PADOS gaps versus the field:**
   - no SSO or organization RBAC (LocalAI has OIDC in OSS; Dify has SSO in Enterprise);
   - no local inference implementation (LocalAI is inference-first);
   - no OTel export (Letta exposes OTEL traces);
   - retrieval backends are in-process only.
4. **AnythingLLM:** docs report auto-approval in non-interactive runs *(snippet)*. PADOS keeps fail-closed authorization for unattended runs.


## Productization V1 update: facts vs. claims vs. conclusions

**Evidence classes.**

- *Verified fact*: checked against raw primary text; the load-bearing items in the method section.
- *Source-backed claim*: from the cited documentation pages; fetch summaries may lose nuance.
- *PADOS design conclusion*: our judgement, not a claim about competitors.

No new competitor facts were added in this update, because no new research was performed. The table above keeps its sources and caveats.

| Conclusion (PADOS design) | Basis |
|---|---|
| **Integrate, don't rebuild:** local inference servers (Ollama, llama.cpp, vLLM, MLX servers, LocalAI) behind the OpenAI-compatible node backend | LocalAI and Ollama are inference-first (source-backed); PADOS implements only the adapter |
| **Integrate:** MCP tools as capabilities under PADOS grants rather than a PADOS-specific tool ecosystem | MCP support is common across the compared projects (source-backed) |
| **Stop rebuilding:** generic chat UI, workflow builder, RAG app UX, and document stores | Dify and AnythingLLM cover these (source-backed); PADOS consumes their outputs as evidence at most |
| **Consider integrating:** OTel export (Letta exposes run traces: source-backed) and signed receipts (OpenSelf signs context receipts: source-backed) | Deferred items in PADOS |
| **Differentiated:** a CAS-protected semantic commit (expected ScopeVersion) plus receipt verification plus immutable audit as the only way to change project state | None of the inspected sources showed an equivalent (limited to the sources inspected; see method) |
| **Differentiated:** evidence-only enterprise capabilities with classification-aware routing to governed local compute nodes | PADOS implementation evidence (enterprise and node reports) |
