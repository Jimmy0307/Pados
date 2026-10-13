"""P6.21M authentication, resource-scope and readback disclosure proofs.

Verdict discipline enforced by these tests:

* AUTHENTICATION  -- a trusted local control-plane caller is authenticated.
* IDENTITY        -- not implemented, and nothing here may imply it.
* RESOURCE SCOPE  -- bounded project/scope consistency against the durable
                     P6.21H run/scope binding.
* MULTI-USER AUTHORIZATION -- not proven and not part of PADOS V1.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pados.config import Settings
from pados.product_loop import authorization
from pados.product_loop.disclosure import (
    REDACTED_CRITERION,
    REDACTED_PATH,
    build_read_view,
    is_physical_path,
    sanitize_criterion_identifier,
    sanitize_text,
)
from pados.product_loop.maker import FileDirectMaker
from pados.product_loop.routes import register_product_loop_routes
from pados.product_loop.schemas import GoalRequest, ProductLoopStatus
from pados.product_loop.service import ProductLoopService
from pados.product_loop.state import InMemoryP6_19BStateGateway
from pados.product_loop.validation import FileValidator

CONTROL_TOKEN = "pados-local-control-" + "token"
BAD_TOKEN = "not-the-control-token"
SCOPE = "p6-21m-scope"
OTHER_SCOPE = "p6-21m-other-scope"


class _CountingMaker(FileDirectMaker):
    """Direct Maker wrapper that records every invocation attempt."""

    def __init__(self, approved_root: Path) -> None:
        super().__init__(approved_root)
        self.calls = 0

    async def make(self, contract: Any, *, run_id: str, attempt: int) -> Any:
        self.calls += 1
        return await super().make(contract, run_id=run_id, attempt=attempt)


def _request(root: Path, **overrides: Any) -> GoalRequest:
    values: dict[str, Any] = {
        "goal": "Apply one bounded approved-workspace change.",
        "workspace_root": str(root),
        "scope_id": SCOPE,
        "changes": [{"path": "app.py", "content": "def result():\n    return 'good'\n"}],
        "acceptance_criteria": [
            {
                "criterion_id": "contains-good",
                "check": "FILE_CONTAINS",
                "target_path": "app.py",
                "expected_text": "return 'good'",
            },
            {"criterion_id": "paths-scoped", "check": "CHANGED_PATHS_SCOPED"},
        ],
        "authorized": True,
        "idempotency_key": f"p6-21m-{uuid4()}",
    }
    values.update(overrides)
    return GoalRequest.model_validate(values)


def _build(tmp_path: Path) -> tuple[FastAPI, ProductLoopService, InMemoryP6_19BStateGateway, _CountingMaker]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    gateway = InMemoryP6_19BStateGateway(tmp_path / "objects")
    maker = _CountingMaker(workspace)
    service = ProductLoopService(
        maker=maker,
        validator=FileValidator(),
        state_gateway=gateway,
        approved_workspace_root=workspace,
    )
    app = FastAPI()
    register_product_loop_routes(app, Settings(), service=service)
    return app, service, gateway, maker


def _headers(
    *, control_value: str | None = CONTROL_TOKEN, scope: str | None = SCOPE
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if control_value is not None:
        headers["X-PADOS-Control-Token"] = control_value
    if scope is not None:
        headers["X-PADOS-Product-Scope"] = scope
    return headers


def _durable_writes(gateway: InMemoryP6_19BStateGateway) -> int:
    return len(gateway.read_index) + len(gateway.receipts) + len(gateway.commits)


def _post(client: TestClient, request: GoalRequest, **header_kwargs: Any):
    return client.post(
        "/product-loop/runs",
        json=request.model_dump(mode="json"),
        headers=_headers(**header_kwargs),
    )


# --- A: disabled feature exposes no surface ---------------------------------


def test_a_feature_disabled_registers_no_product_routes() -> None:
    app = FastAPI()
    register_product_loop_routes(app, Settings())
    paths = [route.path for route in app.routes if "product-loop" in getattr(route, "path", "")]
    assert paths == []
    with TestClient(app) as client:
        assert client.post("/product-loop/runs", json={}).status_code == 404
        assert client.get(f"/product-loop/runs/{uuid4()}").status_code == 404


# --- B/C: authentication precedes Maker and every durable write -------------


def test_b_missing_token_is_rejected_with_no_maker_call_and_no_durable_write(
    tmp_path: Path,
) -> None:
    app, _, gateway, maker = _build(tmp_path)
    with TestClient(app) as client:
        response = _post(client, _request(tmp_path / "workspace"), control_value=None)
    assert response.status_code == 403
    assert response.json()["detail"] == authorization.AUTHENTICATION_FAILED
    assert maker.calls == 0
    assert _durable_writes(gateway) == 0


def test_c_invalid_token_is_rejected_with_no_maker_call_and_no_durable_write(
    tmp_path: Path,
) -> None:
    app, _, gateway, maker = _build(tmp_path)
    with TestClient(app) as client:
        response = _post(client, _request(tmp_path / "workspace"), control_value=BAD_TOKEN)
    assert response.status_code == 403
    assert response.json()["detail"] == authorization.AUTHENTICATION_FAILED
    assert maker.calls == 0
    assert _durable_writes(gateway) == 0


def test_c2_unauthenticated_get_is_rejected_before_readback(tmp_path: Path) -> None:
    app, _, _, _ = _build(tmp_path)
    with TestClient(app) as client:
        created = _post(client, _request(tmp_path / "workspace"))
        run_id = created.json()["run_id"]
        missing_token = client.get(
            f"/product-loop/runs/{run_id}", headers=_headers(control_value=None)
        )
        bad_token = client.get(
            f"/product-loop/runs/{run_id}", headers=_headers(control_value=BAD_TOKEN)
        )
    assert missing_token.status_code == 403
    assert bad_token.status_code == 403
    assert missing_token.json()["detail"] == authorization.AUTHENTICATION_FAILED


def test_c3_authentication_failure_does_not_distinguish_missing_from_invalid(
    tmp_path: Path,
) -> None:
    app, _, _, _ = _build(tmp_path)
    with TestClient(app) as client:
        missing = _post(client, _request(tmp_path / "workspace"), control_value=None)
        invalid = _post(client, _request(tmp_path / "workspace"), control_value=BAD_TOKEN)
    assert missing.json() == invalid.json()


# --- D/E/F: bounded resource scope authorization ----------------------------


def test_d_valid_token_with_wrong_scope_is_rejected(tmp_path: Path) -> None:
    app, _, _, _ = _build(tmp_path)
    with TestClient(app) as client:
        created = _post(client, _request(tmp_path / "workspace"))
        run_id = created.json()["run_id"]
        response = client.get(
            f"/product-loop/runs/{run_id}", headers=_headers(scope=OTHER_SCOPE)
        )
    assert response.status_code == 403
    assert response.json()["detail"] == authorization.RESOURCE_SCOPE_MISMATCH


def test_e_run_id_alone_is_insufficient_for_authorized_readback(tmp_path: Path) -> None:
    app, _, _, _ = _build(tmp_path)
    with TestClient(app) as client:
        created = _post(client, _request(tmp_path / "workspace"))
        run_id = created.json()["run_id"]
        response = client.get(f"/product-loop/runs/{run_id}", headers=_headers(scope=None))
    assert response.status_code == 403
    assert response.json()["detail"] == authorization.RESOURCE_SCOPE_REQUIRED


def test_e2_blank_scope_declaration_is_not_accepted(tmp_path: Path) -> None:
    app, _, _, _ = _build(tmp_path)
    with TestClient(app) as client:
        created = _post(client, _request(tmp_path / "workspace"))
        run_id = created.json()["run_id"]
        response = client.get(f"/product-loop/runs/{run_id}", headers=_headers(scope="   "))
    assert response.status_code == 403
    assert response.json()["detail"] == authorization.RESOURCE_SCOPE_REQUIRED


def test_f_valid_token_and_correct_scope_can_read(tmp_path: Path) -> None:
    app, _, _, _ = _build(tmp_path)
    with TestClient(app) as client:
        created = _post(client, _request(tmp_path / "workspace"))
        run_id = created.json()["run_id"]
        response = client.get(f"/product-loop/runs/{run_id}", headers=_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == run_id
    assert payload["scope_id"] == SCOPE
    assert payload["status"] == ProductLoopStatus.COMMITTED.value


def test_f2_post_requires_declared_scope_matching_the_contract_scope(tmp_path: Path) -> None:
    app, _, gateway, maker = _build(tmp_path)
    with TestClient(app) as client:
        missing = _post(client, _request(tmp_path / "workspace"), scope=None)
        mismatched = _post(client, _request(tmp_path / "workspace"), scope=OTHER_SCOPE)
    assert missing.status_code == 403
    assert missing.json()["detail"] == authorization.RESOURCE_SCOPE_REQUIRED
    assert mismatched.status_code == 403
    assert mismatched.json()["detail"] == authorization.RESOURCE_SCOPE_MISMATCH
    assert maker.calls == 0
    assert _durable_writes(gateway) == 0


def test_f3_unknown_run_is_not_distinguishable_by_scope_guessing(tmp_path: Path) -> None:
    app, _, _, _ = _build(tmp_path)
    with TestClient(app) as client:
        response = client.get(f"/product-loop/runs/{uuid4()}", headers=_headers())
        malformed = client.get("/product-loop/runs/not-a-run-id", headers=_headers())
    assert response.status_code == 404
    assert response.json()["detail"] == authorization.PRODUCT_RUN_NOT_FOUND
    assert malformed.status_code == 404
    assert malformed.json()["detail"] == authorization.PRODUCT_RUN_NOT_FOUND


def test_f4_idempotent_replay_cannot_disclose_another_scope_run(tmp_path: Path) -> None:
    """A replayed idempotency key must not project a run from a different scope."""
    app, _, _, _ = _build(tmp_path)
    shared_key = "p6-21m-shared-idempotency-key"
    first = _request(tmp_path / "workspace", idempotency_key=shared_key)
    replay = _request(
        tmp_path / "workspace", idempotency_key=shared_key, scope_id=OTHER_SCOPE
    )
    with TestClient(app) as client:
        created = _post(client, first)
        assert created.status_code == 200
        assert created.json()["scope_id"] == SCOPE
        response = _post(client, replay, scope=OTHER_SCOPE)
    assert response.status_code == 403
    assert response.json()["detail"] == authorization.RESOURCE_SCOPE_MISMATCH


# --- G: workspace root fails before Maker and before durable mutation -------


@pytest.mark.parametrize(
    "candidate",
    [
        "C:/PADOS",
        "C:/PADOS_WORKTREES/other",
        "C:/PADOS_ARCHITECTURE_AUDIT",
        "D:/elsewhere",
        "/etc",
        "relative/path",
        "C:tmp",
        "",
        "   ",
    ],
)
def test_g_arbitrary_workspace_root_is_rejected_before_maker(
    tmp_path: Path, candidate: str
) -> None:
    app, _, gateway, maker = _build(tmp_path)
    with TestClient(app) as client:
        response = _post(client, _request(tmp_path / "workspace", workspace_root=candidate or "x"))
        if candidate.strip():
            response = client.post(
                "/product-loop/runs",
                json=_request(tmp_path / "workspace").model_dump(mode="json")
                | {"workspace_root": candidate},
                headers=_headers(),
            )
    assert response.status_code == 403
    assert response.json()["detail"] == authorization.INVALID_WORKSPACE_SCOPE
    assert maker.calls == 0
    assert _durable_writes(gateway) == 0


def test_g2_parent_and_traversal_equivalent_roots_are_rejected(tmp_path: Path) -> None:
    app, _, gateway, maker = _build(tmp_path)
    workspace = tmp_path / "workspace"
    candidates = [
        str(tmp_path),
        str(workspace / ".." / ".." / "workspace"),
        str(workspace / "nested"),
        str(tmp_path / "workspace_sibling"),
    ]
    with TestClient(app) as client:
        for candidate in candidates:
            response = client.post(
                "/product-loop/runs",
                json=_request(workspace).model_dump(mode="json")
                | {"workspace_root": candidate},
                headers=_headers(),
            )
            assert response.status_code == 403, candidate
            assert response.json()["detail"] == authorization.INVALID_WORKSPACE_SCOPE
    assert maker.calls == 0
    assert _durable_writes(gateway) == 0


def test_g3_service_layer_rejects_unapproved_root_without_any_durable_write(
    tmp_path: Path,
) -> None:
    _, service, gateway, maker = _build(tmp_path)
    request = _request(tmp_path / "workspace", workspace_root=str(tmp_path))
    with pytest.raises(authorization.ProductAuthorizationError) as excinfo:
        asyncio.run(service.execute(request))
    assert excinfo.value.code == authorization.INVALID_WORKSPACE_SCOPE
    assert maker.calls == 0
    assert _durable_writes(gateway) == 0


def test_g4_maker_keeps_its_own_independent_approved_root_check(tmp_path: Path) -> None:
    """The early gate is defense in depth; the Maker check must not be weakened."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    gateway = InMemoryP6_19BStateGateway(tmp_path / "objects")
    unguarded = ProductLoopService(
        maker=_CountingMaker(workspace),
        validator=FileValidator(),
        state_gateway=gateway,
    )
    other = tmp_path / "other"
    other.mkdir(parents=True, exist_ok=True)
    report = asyncio.run(unguarded.execute(_request(workspace, workspace_root=str(other))))
    assert report.status == ProductLoopStatus.AUTHORITY_BLOCK
    assert report.final_result is not None
    assert report.final_result.changed_paths == []
    assert not (other / "app.py").exists()


# --- H: the frozen loop still works through the hardened surface ------------


def test_h_valid_post_with_approved_root_keeps_the_frozen_loop_functional(
    tmp_path: Path,
) -> None:
    app, _, _, maker = _build(tmp_path)
    workspace = tmp_path / "workspace"
    with TestClient(app) as client:
        response = _post(client, _request(workspace))
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == ProductLoopStatus.COMMITTED.value
    assert payload["semantic_commit_status"] == "COMMITTED"
    assert maker.calls == 1
    assert (workspace / "app.py").read_text(encoding="utf-8") == (
        "def result():\n    return 'good'\n"
    )


# --- I/J/K/L: readback disclosure policy ------------------------------------


def _committed_view(tmp_path: Path) -> dict[str, Any]:
    app, _, _, _ = _build(tmp_path)
    with TestClient(app) as client:
        created = _post(client, _request(tmp_path / "workspace"))
        run_id = created.json()["run_id"]
        response = client.get(f"/product-loop/runs/{run_id}", headers=_headers())
    assert response.status_code == 200
    return response.json()


def _disclosed_body(payload: dict[str, Any]) -> str:
    """Serialize everything the caller receives except the withheld-field manifest."""
    return json.dumps({key: value for key, value in payload.items() if key != "withheld_fields"})


def test_i_normal_get_discloses_no_absolute_physical_workspace_root(tmp_path: Path) -> None:
    payload = _committed_view(tmp_path)
    body = _disclosed_body(payload)
    assert str(tmp_path) not in body
    assert (tmp_path / "workspace").as_posix() not in body
    assert "workspace_root" not in body
    assert payload["authorized_contract"]["workspace_scope"] == "APPROVED_WORKSPACE"
    for contract in payload["contracts"]:
        assert "workspace_root" not in contract


def test_j_normal_get_discloses_no_physical_allowed_root(tmp_path: Path) -> None:
    payload = _committed_view(tmp_path)
    for contract in payload["contracts"]:
        for candidate in contract["allowed_paths"]:
            assert not is_physical_path(candidate) or candidate == REDACTED_PATH
    assert "contract.physical_allowed_paths" in payload["withheld_fields"]


def test_k_normal_get_exposes_no_raw_storage_uri(tmp_path: Path) -> None:
    payload = _committed_view(tmp_path)
    body = _disclosed_body(payload)
    assert "storage_uri" not in body
    reference = payload["report_reference"]
    assert reference is None or set(reference) <= {
        "object_id",
        "content_hash",
        "media_type",
        "size_bytes",
        "object_kind",
    }


def test_l_project_relative_changed_paths_remain_useful(tmp_path: Path) -> None:
    payload = _committed_view(tmp_path)
    assert payload["accepted_result"]["changed_paths"] == ["app.py"]
    assert payload["attempts"][0]["changed_paths"] == ["app.py"]
    assert payload["contracts"][0]["allowed_paths"] == ["app.py"]
    assert payload["attempts"][0]["input_manifest_hash"]
    assert payload["attempts"][0]["receipt_id"]
    assert payload["latest_validation"]["validation_passed"] is True


def test_l2_physical_paths_are_classified_and_only_those_are_redacted() -> None:
    assert is_physical_path("C:/PADOS/file.py") is True
    assert is_physical_path("/etc/passwd") is True
    assert is_physical_path("../escape.py") is True
    assert is_physical_path("~/secret.py") is True
    assert is_physical_path("src/pados/app.py") is False
    assert is_physical_path("app.py") is False


@pytest.mark.parametrize(
    "physical_value",
    [
        "/etc",
        "/etc/passwd",
        "/home/user/secret",
        "/home/user/data",
        "/var/tmp/pados",
        "/var/tmp/file",
        "/tmp/pados/run",
        "/mnt/c/PADOS/secret",
        r"C:\PADOS\secret",
        "C:/PADOS/secret",
        r"\\server\share\secret",
        "//server/share/secret",
        "file:///C:/PADOS/secret",
        "file:///etc/passwd",
    ],
)
def test_r01_to_r09_and_r18_physical_paths_are_redacted(physical_value: str) -> None:
    scrubbed = sanitize_text(f"failure at {physical_value}")
    assert physical_value not in (scrubbed or "")
    assert REDACTED_PATH in (scrubbed or "")


@pytest.mark.parametrize("safe_value", ["src/pados/product_loop/routes.py", "docs/report.md"])
def test_r13_to_r14_safe_project_relative_paths_remain_visible(safe_value: str) -> None:
    assert sanitize_text(safe_value) == safe_value


def test_r18_normal_http_urls_are_not_broadened_into_path_redaction() -> None:
    value = "see https://example.com/path for the public reference"
    assert sanitize_text(value) == value


@pytest.mark.parametrize(
    ("criterion_id", "expected"),
    [
        ("C:/PADOS/secret", REDACTED_CRITERION),
        ("/etc/passwd", REDACTED_CRITERION),
        ("criterion-1", "criterion-1"),
        ("s3://internal-bucket/object", REDACTED_CRITERION),
    ],
)
def test_r10_to_r12_criterion_identifiers_are_sanitized_only_in_projection(
    criterion_id: str, expected: str
) -> None:
    assert sanitize_criterion_identifier(criterion_id) == expected


def test_r10_r11_r12_r15_r16_projection_redacts_criteria_without_mutating_records(
    tmp_path: Path,
) -> None:
    app, service, gateway, _ = _build(tmp_path)
    workspace = tmp_path / "workspace"
    criterion_ids = ("C:/PADOS/secret", "/etc/passwd", "criterion-1")
    criteria = [
        {
            "criterion_id": criterion_id,
            "check": "CHANGED_PATHS_SCOPED",
        }
        for criterion_id in criterion_ids
    ]

    with TestClient(app) as client:
        created = _post(
            client,
            _request(workspace, acceptance_criteria=criteria),
        )
        assert created.status_code == 200
        run_id = created.json()["run_id"]
        disclosed = client.get(f"/product-loop/runs/{run_id}", headers=_headers())
    assert disclosed.status_code == 200
    disclosed_contract = disclosed.json()["authorized_contract"]
    assert disclosed_contract["acceptance_criteria"] == [
        REDACTED_CRITERION,
        REDACTED_CRITERION,
        "criterion-1",
    ]

    durable = service.read_run(run_id)
    assert durable is not None
    durable_before = durable.model_dump(mode="json")
    report_ref_before = gateway.read_index[run_id]["report_ref"]
    report_bytes_before = gateway.object_store.read(report_ref_before)
    receipts_before = {
        key: value.model_dump(mode="json") for key, value in gateway.receipts.items()
    }

    view = build_read_view(durable, scope_id=SCOPE)

    assert view.authorized_contract.acceptance_criteria == (
        REDACTED_CRITERION,
        REDACTED_CRITERION,
        "criterion-1",
    )
    assert durable.authorized_contract.acceptance_criteria == criterion_ids
    assert durable.model_dump(mode="json") == durable_before
    assert gateway.read_index[run_id]["report_ref"] == report_ref_before
    assert gateway.object_store.read(report_ref_before) == report_bytes_before
    assert {
        key: value.model_dump(mode="json") for key, value in gateway.receipts.items()
    } == receipts_before


# --- M: bounded failure responses -------------------------------------------


def test_m_failure_response_is_bounded_without_path_or_traceback_leakage(
    tmp_path: Path,
) -> None:
    app, _, _, _ = _build(tmp_path)
    secret_root = "C:/PADOS/formal/secret-location"
    with TestClient(app) as client:
        response = client.post(
            "/product-loop/runs",
            json=_request(tmp_path / "workspace").model_dump(mode="json")
            | {"workspace_root": secret_root},
            headers=_headers(),
        )
    body = response.text
    assert response.status_code == 403
    assert "withheld_fields" not in body
    assert response.json()["detail"] == authorization.INVALID_WORKSPACE_SCOPE
    assert secret_root not in body
    assert "Traceback" not in body
    assert "File \"" not in body


def test_m2_internal_exception_text_is_scrubbed_of_physical_locations() -> None:
    message = "target could not be read: [Errno 2] No such file: 'C:/PADOS/runtime/app.py'"
    scrubbed = sanitize_text(message)
    assert scrubbed is not None
    assert "C:/PADOS" not in scrubbed
    assert REDACTED_PATH in scrubbed
    assert "target could not be read" in scrubbed
    posix = sanitize_text("failed at /home/operator/pados/app.py while reading")
    assert posix is not None
    assert "/home/operator" not in posix
    assert "while reading" in posix


# --- N: canonical records are never mutated by presentation sanitization ----


def test_n_canonical_receipt_and_object_records_are_unchanged(tmp_path: Path) -> None:
    app, service, gateway, _ = _build(tmp_path)
    workspace = tmp_path / "workspace"
    with TestClient(app) as client:
        created = _post(client, _request(workspace))
        run_id = created.json()["run_id"]
        client.get(f"/product-loop/runs/{run_id}", headers=_headers())

    durable = service.read_run(run_id)
    assert durable is not None
    # The canonical durable read model keeps the physical facts...
    assert durable.authorized_contract.workspace_root == str(workspace.resolve())
    assert durable.report_object_ref is not None
    assert durable.report_object_ref["storage_uri"]
    # ...and the presentation view is the only thing that withholds them.
    view = build_read_view(durable, scope_id=SCOPE)
    assert "workspace_root" not in view.model_dump(mode="json")["authorized_contract"]
    assert view.report_reference is not None
    assert "storage_uri" not in view.model_dump(mode="json")["report_reference"]

    entry = gateway.read_index[run_id]
    stored = json.loads(gateway.object_store.read(entry["report_ref"]).decode("utf-8"))
    assert stored["contracts"][0]["scope"]["workspace_root"] == str(workspace.resolve())
    assert stored["report_object_ref"] is None or "storage_uri" in str(stored)
    receipt = next(iter(gateway.receipts.values()))
    assert receipt.input_manifest_hash
    assert receipt.output_content_hash


# --- O/P/Q: authentication grants no semantic authority ---------------------


def test_o_authentication_does_not_bypass_the_executable_work_contract(
    tmp_path: Path,
) -> None:
    app, _, _, maker = _build(tmp_path)
    with TestClient(app) as client:
        response = _post(client, _request(tmp_path / "workspace", authorized=False))
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == ProductLoopStatus.AUTHORITY_BLOCK.value
    assert payload["semantic_commit_status"] != "COMMITTED"
    assert payload["human_action_required"] is True
    assert maker.calls == 0


def test_o2_authentication_does_not_widen_the_contract_allowlist(tmp_path: Path) -> None:
    app, _, _, _ = _build(tmp_path)
    workspace = tmp_path / "workspace"
    request = _request(
        workspace,
        changes=[{"path": "app.py", "content": "x = 1\n"}],
        acceptance_criteria=[{"criterion_id": "paths-scoped", "check": "CHANGED_PATHS_SCOPED"}],
    )
    with TestClient(app) as client:
        payload = _post(client, request).json()
    contract = payload["contracts"][0]
    assert contract["allowed_paths"] == ["app.py"]
    assert "WRITE_FORMAL_REPO" in contract["prohibited_actions"]
    assert "MERGE" in contract["prohibited_actions"]
    assert "PUSH" in contract["prohibited_actions"]
    assert "DEPLOY" in contract["prohibited_actions"]


def test_p_authentication_does_not_bypass_scope_version_cas(tmp_path: Path) -> None:
    app, service, gateway, _ = _build(tmp_path)
    class ConcurrentMaker(_CountingMaker):
        async def make(self, contract: Any, *, run_id: str, attempt: int) -> Any:
            gateway.versions[SCOPE] = 7  # Advance after bounded context capture.
            return await super().make(contract, run_id=run_id, attempt=attempt)

    service.maker = ConcurrentMaker(tmp_path / "workspace")
    with TestClient(app) as client:
        response = _post(client, _request(tmp_path / "workspace"))
    payload = response.json()
    assert payload["status"] == ProductLoopStatus.STALE_CANDIDATE.value
    assert payload["semantic_commit_status"] != "COMMITTED"
    assert gateway.versions[SCOPE] == 7


def test_q_authentication_does_not_promote_memory_or_raise_authority(
    tmp_path: Path,
) -> None:
    app, _, gateway, _ = _build(tmp_path)
    with TestClient(app) as client:
        payload = _post(client, _request(tmp_path / "workspace")).json()
    assert payload["memory_authority"] == "CANDIDATE"
    assert payload["graph_authority"] == "PROPOSAL_ONLY"
    assert payload["semantic_authority"] == "READ_MODEL_IS_NOT_SEMANTIC_AUTHORITY"
    assert not hasattr(gateway, "promote_memory")
    assert not hasattr(gateway, "confirm_graph")


def test_q2_control_token_carries_no_identity_or_tenancy_concept() -> None:
    source = Path(authorization.__file__).read_text(encoding="utf-8")
    for forbidden in ("user_id", "organization_id", "tenant", "role_name", "rbac"):
        assert forbidden not in source.lower().replace("rbac framework", "")


# --- shared control-plane auth seam compatibility ---------------------------


def test_r_extracted_control_auth_seam_preserves_existing_behaviour() -> None:
    from pados.control import security as control_security

    settings = Settings()
    with pytest.raises(PermissionError, match="missing"):
        control_security.authorize_control_request(settings, None)
    with pytest.raises(PermissionError, match="invalid"):
        control_security.authorize_control_request(settings, BAD_TOKEN)
    control_security.authorize_control_request(settings, CONTROL_TOKEN)
    assert control_security.CONTROL_TOKEN_HEADER == "X-PADOS-Control-Token"


def test_r2_existing_control_endpoints_still_require_the_token() -> None:
    from pados.api.app import create_app

    with TestClient(create_app()) as client:
        assert client.get("/control/projects").status_code == 403
        assert client.get("/control/health").status_code == 200
