# PADOS

**Intelligent Mindmap-driven Portable Cognitive & Execution Operating System**. Status: `0.1.0a1` (**alpha**, not production-ready). License: AGPL-3.0-only, with commercial licensing available from the copyright holder (see `LICENSE`, `LICENSING.md`, `COMMERCIAL.md`, `THIRD_PARTY_NOTICES.md`).

## 1. What PADOS is

PADOS is a local-first control plane for work done by people, AI agents and models on real projects. It keeps an authoritative, versioned project state (goals, decisions, context, memory, accepted results) and lets capabilities act on it only through a governed, verifiable chain.

PADOS is **not** primarily a chat UI, an agent framework, an Ollama wrapper, a RAG application, a workflow builder or a project-management clone. It uses those kinds of tools as *capability providers*.

## 2. The problem

Agent and model output is easy to produce and hard to trust. Tools often let a model's claim of success directly change project state. PADOS separates **doing** from **accepting**.

## 3. Architecture (execution model)

```text
Human Intent → Project Context → Governed Work Package → Capability / Provider / Compute Node
→ ExecutionReceipt → Verification → ResultReceipt → Validation → ScopeVersion → CAS
→ Immutable Audit → Updated Project State
```

- **Two database lanes:** the operational lane holds projects, goals, decisions and memory authority; a separate proof lane holds ScopeVersion/CAS, receipts and the hash-chained semantic audit.
- **Views:** Mission Control and the Decision Inbox are read-only projections of those authorities.

## 4. Semantic authority model

- `MODEL OUTPUT != TRUTH`
- `EXECUTION COMPLETE != SEMANTIC ACCEPTANCE`

A result changes project state only if its ExecutionReceipt verifies against the artifacts, deterministic validation passes, and a compare-and-swap on the expected ScopeVersion succeeds. Every accepted change is appended to an immutable, hash-chained audit.

## 5. Why agents and models are not truth

Providers (Codex CLI, Claude Code, local models, MCP tools, retrieval engines) can be wrong, stale, prompt-injected or compromised. PADOS treats their output as data: context text cannot grant permissions, grants are project-bound, and artifacts are hash-verified independently.

## 6. Local and frontier providers

The Worker Router selects among frontier CLI agents and local models. Local models run on **Local Compute Nodes**; frontier agents run in isolated workspaces with declared output paths.

## 7. Enterprise Capability Pack (generic contracts)

- Capability manifests and a data classification (PUBLIC → RESTRICTED).
- Two-stage authorization, retrieval, reranking, source lineage with revocation planning, evidence budgets, SecretRef credentials, and content-free observability.
- Evidence enters a work package's context; it never becomes truth by itself.

## 8. Local Compute Nodes

`pados node serve` turns a machine into an execution substrate: a Windows RTX laptop, a Linux GPU box, a CPU machine, or a future Mac mini. Nodes speak a small authenticated protocol (`pados.compute-node.protocol.v1`) and use OpenAI-compatible, Ollama or llama.cpp backends; MLX is a contract only. Every node result is hash-verified and bound to a receipt. Nodes are never authorities.

## 9. Quick start

See `docs/product/QUICKSTART.md`. The one-command demo is `bash scripts/demo/run_demo.sh` (add `--local-ai` to use a local Ollama).

## 10. Current limitations

- **Security profiles:** single-user and trusted-LAN only. Node identity uses HMAC over a shared credential; there is no transport encryption between PADOS and nodes; hardened (asymmetric) identity is designed but not implemented.
- **Codex CLI on Windows** currently lacks a workspace-write sandbox (an external provider limitation).
- **Not implemented yet:**
  - Apple Silicon/MLX has no live validation;
  - no production OCR engine;
  - no SSO or multi-user RBAC.
- **API stability:** most APIs are experimental in 0.x.

## 11. Security model

See `docs/security/THREAT_MODEL_V1.md` and `docs/security/SECURITY_PROFILES.md`. The control API binds to loopback; `pados init` creates a unique control token; credentials are SecretRefs. **Do not expose PADOS or nodes to the public Internet.**

## 12. Maturity

Alpha. The governed chain, enterprise contracts and compute-node protocol are covered by automated gates: Python, frontend, Playwright, Windows-native suites, security scans and deterministic vertical slices. Semantics may still change before 1.0 (docs/product/COMPATIBILITY_POLICY.md).

## License

PADOS Core is licensed under the [GNU Affero General Public License v3.0 only](LICENSE) (`SPDX-License-Identifier: AGPL-3.0-only`). The copyright holder may also offer a separate commercial license (`COMMERCIAL.md`). Future public SDKs or protocol schemas may be Apache-2.0 only in a separate `sdk/` directory with their own `LICENSE`. Third-party components keep their own licenses; see `THIRD_PARTY_NOTICES.md`. "PADOS" and the PADOS logo are reserved: the code license grants no trademark rights, and forks must not present themselves as the official PADOS product (`LICENSING.md`).

PADOS has been informed by and validated against real-world enterprise local-AI and structured problem-solving deployments.

## Contributing

Contributions will require a signed CLA once published; until then external code contributions are not accepted. See `CONTRIBUTING.md`. Security issues: see `SECURITY.md`.
