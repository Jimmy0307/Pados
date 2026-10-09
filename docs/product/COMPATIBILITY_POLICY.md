# Backward compatibility policy

| Area | Before 1.0 (0.x alpha/beta) | From 1.0 (stable) |
|---|---|---|
| Configuration (`.env`, node config, node registry, enterprise config) | Keys may be renamed with a changelog note and a doctor warning for one minor release | Additive only within a major; removals need a deprecation release |
| Database migrations | Forward-only; no historical revision is edited; upgrade procedure documented per release; destructive changes need a documented backup step | Same, plus a tested upgrade path from the previous two minors |
| Node protocol (`…protocol.v1`) | Additive changes only within v1; breaking changes create v2, and nodes and PADOS negotiate by identifier | PADOS supports the current and previous protocol major |
| Receipts (Core and node) | **Stable semantics now**: hashed fields never change within a schema version, and old receipts stay verifiable | Same |
| Provider adapters (backends) | Adapter kinds may be added; a kind is not removed without a deprecation note | Same |
| Capability manifests | v1 fields stable; new optional fields allowed | Same |
| Python SDK (`pados.enterprise`, `pados.compute_node`) | PUBLIC_EXPERIMENTAL: may change per minor with notes | PUBLIC_STABLE subset frozen per major |
| HTTP API | `/health`, `/ready` stable; everything else experimental | Documented stable subset versioned |

**Invariant in every version:** `MODEL OUTPUT != TRUTH` and `EXECUTION COMPLETE != SEMANTIC ACCEPTANCE`. No release may add a path that advances semantic state without receipt verification, validation and CAS.
