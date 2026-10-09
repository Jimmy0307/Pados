# Enterprise authority boundary

## Invariants

- `MODEL OUTPUT != TRUTH`; `EXECUTION COMPLETE != SEMANTIC ACCEPTANCE`.
- Enterprise capabilities are providers of the following. None of them can write PADOS semantic state:
  - evidence packages
  - extraction artifacts
  - rerank scores
  - tool results
- The pack imports no semantic-authority module (`pados.state_proof`, `pados.memory.service`/`repository`, `pados.product_loop.state`, `sqlalchemy`). This is enforced by `tests/enterprise/test_enterprise_context_and_fail_closed.py::test_enterprise_package_cannot_import_semantic_authority`.
- Every manifest's `ArtifactContract.semantic_authority` must be `NONE`. Anything else is rejected at validation.
- Evidence reaches PADOS only as `bounded_context.extensions.enterprise_evidence` of a governed run. It is:
  - hashed into the Work Contract context fingerprint;
  - persisted in the proof-lane run-context object;
  - marked `semantic_authority: NONE` and `instructions_are_data_not_permission: true`.

## One authorization authority

| Question | Authority | Where |
|---|---|---|
| May this Work Contract use capability X? | PADOS project grant (`authorize_execution_capabilities`) | `authorize_capability` adds `ENTERPRISE:<capability_id>` to the contract's `required_capabilities` (and `mcp_capabilities` for `MCP_TOOL`) and the manifest's SecretRefs to `secret_refs`, then delegates |
| Is X installed? | `CapabilityRegistry` (description only) | Installed ≠ authorized; health must be `READY` |
| Who is the caller and what is their clearance? | Enterprise identity adapter input (`AccessContext`) | Not persisted by PADOS |
| Which principals/roles may see a source? | The source's system of record, carried on `EnterpriseSource` | Evaluated per request; there is no ACL table |

PADOS has no second ACL store and no role → grant mapping. An agent role label is not a grant, which matches the existing `agent_role` rule.

## Two stages

1. **Retrieval authorization → coarse eligibility.** `RetrievalScope` requires:
   - tenant match;
   - project match;
   - classification ≤ lowest(caller clearance, capability maximum, request ceiling);
   - source id/type filters pass;
   - revoked projections are excluded.

   Every backend applies the scope *before* scoring.
2. **Presentation/execution authorization → fine enforcement.** `presentation_filter` runs per item. It requires:
   - source identity present;
   - item inside the scope;
   - clearance sufficient;
   - source ACL allows (principal or role).

   RESTRICTED sources always need an explicit principal or role grant. This runs **before reranking**, so no reranker (local or HTTP) sees withheld text. Withheld items are counted by reason; their text never enters the package or any log.

After generation, `assert_no_withheld_content(output, pipeline.unauthorized_texts(...))` rejects any output that reproduces a 6-word run of text the caller may not see (`WITHHELD_EVIDENCE_LEAK`).

## Fail-closed behaviours (tested)

| Condition | Result |
|---|---|
| No grant, wrong project, wrong work package | `CAPABILITY_NOT_AUTHORIZED` / `GRANT_SCOPE_MISMATCH` / `GRANT_WORK_PACKAGE_MISMATCH` |
| MCP tool without an MCP grant | `MCP_CAPABILITY_NOT_AUTHORIZED` (denied by default) |
| Capability not `READY` | `CAPABILITY_<HEALTH>` |
| Evidence context for another scope | `CONTEXT_EXTENSION_UNAUTHORIZED` before any run id, receipt, read index or file (vertical slice step 8: proof rows unchanged) |
| Budget exceeded | `EVIDENCE_BUDGET_EXCEEDED` before the provider is invoked |
| OCR not configured, empty PDF text layer | `OCR_NOT_CONFIGURED` / `PDF_TEXT_LAYER_EMPTY_OCR_REQUIRED` |
| ACTIVE reranker failure | `RerankError` (no silent fallback); SHADOW failures are only reported |
| Remote-egress capability without approval | Doctor `ENTERPRISE_CAPABILITY_UNAUTHORIZED` |

## SecretRef

- Manifests accept only `secretref://…` references and reject credential-shaped values anywhere.
- `SecretBoundary.resolved(refs, authorized_refs=grant.secret_refs)` yields `SecretValue` objects that render as `[REDACTED]`, and clears them on exit.
- ExecutionReceipts carry no secret material (tested).
- Transport errors are passed through `redact`.
- Observability rejects non-allowlisted attributes, so a secret cannot be logged through it.
