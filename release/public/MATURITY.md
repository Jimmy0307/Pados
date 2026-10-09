# Release maturity statement: 0.1.0a1 (ALPHA)

PADOS 0.1.0a1 is an **alpha** release. It is **not production-ready**.

**What is proven by automated gates**

- The governed execution chain: context → work package → provider or compute node → ExecutionReceipt → verification → ResultReceipt → validation → ScopeVersion CAS → immutable audit.
- The Enterprise Capability Pack contracts and the Local Compute Node protocol, on Linux and Windows.
- The one-command deterministic demo.

**What is not provided**

- Internet-facing deployment, SSO or multi-user RBAC.
- Hardened (asymmetric) node identity; transport encryption between PADOS and nodes.
- Stable API guarantees: most APIs are experimental in 0.x.
- Apple Silicon/MLX validation; a production OCR engine.

**Where to find details:** version and compatibility policy in docs/product/VERSIONING_POLICY.md and docs/product/COMPATIBILITY_POLICY.md; known limitations and residual risks in docs/security/THREAT_MODEL_V1.md.
