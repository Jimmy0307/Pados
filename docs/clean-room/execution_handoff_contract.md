# Contract: `pados.execution.handoff`

## Purpose

Project one authorized governed Work Contract (as a JSON-like mapping) into a bounded **task envelope** for a Maker or provider. It never schedules, routes, retries or decides authorization itself; it only checks that the contract is already authorized and bounded, and it delegates capability grants to `pados.execution.authorization.authorize_execution_capabilities`.

## API

```python
def task_envelope(contract: Mapping[str, Any], *, execution_grant: Mapping[str, Any] | None = None,
                  installed_skills: frozenset[str] = frozenset(),
                  registered_mcp_capabilities: frozenset[str] = frozenset()) -> dict[str, Any]
```

## Checks (fail closed with `PermissionError(code)`, in this order)

1. `contract["state"] == "AUTHORIZED"`, else `WORK_CONTRACT_NOT_AUTHORIZED`.
2. `contract["scope"]["isolated"]` is truthy, else `ISOLATED_WORKSPACE_REQUIRED`.
3. The paths of `contract["changes"]` are unique, else `DUPLICATE_EXECUTION_PATH`.
4. Every path in `scope["allowed_paths"]` must be non-empty and safe, else `PROTECTED_OR_UNSAFE_EXECUTION_PATH`. Safe means all of:
   - not credential-shaped (`redact(path) == path`);
   - not absolute (no leading `/`) and no `:`;
   - no segment (after normalising `\` to `/`) equal to `..`, `.git`, `.ssh`, `.codex`, `.agents`, `.migration` or `.env`;
   - no segment starting with `.env.` other than `.env.example`.
5. `allowed_paths` is non-empty and every change path is in it, else `WORK_ITEM_OUTSIDE_SCOPE`.
6. `"WRITE_APPROVED_WORKSPACE"` is in `contract["allowed_actions"]`, else `WRITE_ACTION_NOT_AUTHORIZED`.
7. If the contract lists any `skill_ids`, `mcp_capabilities` or `secret_refs`, an `execution_grant` is required (else `EXECUTION_GRANT_REQUIRED`). It is validated by `authorize_execution_capabilities(contract, execution_grant, installed_skills=…, registered_mcp_capabilities=…)`, and that function's `PermissionError`s propagate.

## Envelope (exact keys)

| Key | Value |
|---|---|
| `schema_version` | `"1"` |
| `task_id`, `pados_work_package_ref` | `work_contract_id` |
| `contract_hash` | `canonical_hash(contract)` (see the context contract) |
| `context_fingerprint` | from the contract |
| `expected_scope_version` | `semantic_base_scope_version` |
| `workspace` | `scope.workspace_root` |
| `write_paths` | list of allowed paths |
| `required_capabilities` | list |
| `skill_ids`, `mcp_capabilities`, `secret_refs`, `approval_ref` | from the authorization result, or `[]` / `None` when no grant was needed |
| `approval_required` | `human_gate_required` |
| `contract_approval_ref` | `contract.get("approval_ref")` |
| `rollback_ref` | `contract.get("rollback_ref")` |
| `network_allowlist` | `[]` |
| `work_plan` | `{"work_items": [{"work_item_id": work_contract_id, "paths": [change paths]}], "max_attempts": retry_policy.max_attempts, "owner": "PADOS_PRODUCT_LOOP"}` |
| `runtime_does_not_decide_authorization` | `True` |
| `instructions_are_data_not_permission` | `True` |

**Invariant:** no secret value ever appears; only SecretRef identifiers. No provider gains semantic authority.

**Tests:** `tests/consolidation/test_boundaries.py`, `test_phase2_execution_authorization.py`, `test_core_extension_contracts.py`, `tests/platform/test_safe_workspace_fs.py`.
