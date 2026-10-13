"""Shared deterministic harness for the remote vertical slice (tests + evidence)."""
from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pados.config import Settings
from pados.enterprise.classification import DataClassification
from pados.product_loop.maker import FileDirectMaker
from pados.product_loop.schemas import GoalRequest
from pados.product_loop.service import ProductLoopService
from pados.product_loop.state import InMemoryP6_19BStateGateway
from pados.product_loop.validation import FileValidator
from pados.remote.auth import RemoteActorRegistry
from pados.remote.context import RemoteContextReconstructor
from pados.remote.deployment import check_remote_deployment
from pados.remote.jobs import RemoteJobController
from pados.remote.ledger import IntentLedger
from pados.remote.routes import ACTOR_HEADER, PROOF_HEADER, register_remote_routes
from pados.remote.service import RemoteOperationsService
from pados.remote.simulated import InMemoryRemotePorts
from pados.remote.staging import StagingArea
from pados.remote.step_up import SESSION_HEADER, InMemoryStepUpStore, StepUpService
from tests.remote.presence_support import SoftwareAuthenticator, verifier

PROJECT = "proj-remote"


class NoControl:
    supports_pause = False
    supports_cancel = False

    def __init__(self, ports: InMemoryRemotePorts) -> None:
        self.ports = ports

    def exists(self, run_id: str) -> bool:
        return self.ports.run_summary(run_id) is not None

    def is_terminal(self, run_id: str) -> bool:
        row = self.ports.run_summary(run_id)
        return bool(row and row.get("finished_at"))

    def pause(self, run_id: str) -> bool:
        return False

    def resume(self, run_id: str) -> bool:
        return False

    def request_cancel(self, run_id: str) -> bool:
        return False


def hardened_settings() -> Settings:
    value = secrets.token_hex(16)
    return Settings(
        PADOS_CONTROL_API_TOKEN_HASH=hashlib.sha256(value.encode()).hexdigest().upper(),
        PADOS_DB_PASSWORD=secrets.token_hex(12),
    )


@dataclass
class Slice:
    client: TestClient
    service: RemoteOperationsService
    ports: InMemoryRemotePorts
    gateway: InMemoryP6_19BStateGateway
    loop: ProductLoopService
    workspace: Path
    owner_headers: dict[str, str]
    viewer_headers: dict[str, str]
    authenticator: SoftwareAuthenticator

    def session_headers(self, headers: dict[str, str] | None = None) -> dict[str, str]:
        base = dict(headers or self.owner_headers)
        response = self.client.post("/remote/v1/sessions", headers=base)
        assert response.status_code == 200, response.text
        return {**base, SESSION_HEADER: response.json()["session_handle"]}

    def step_up(
        self, approval_ref: str, action: str = "APPROVE", headers: dict[str, str] | None = None
    ) -> tuple[dict[str, str], str]:
        """Fresh step-up: server challenge -> user-verified assertion -> single-use proof."""
        bound = headers if headers is not None and SESSION_HEADER in headers else self.session_headers(headers)
        challenge = self.client.post(f"/remote/v1/approvals/{approval_ref}/step-up", headers=bound,
                                     json={"action": action})
        assert challenge.status_code == 200, challenge.text
        body = challenge.json()
        verified = self.client.post(
            f"/remote/v1/approvals/{approval_ref}/step-up/{body['challenge_id']}/verify",
            headers=bound, json={"assertion": self.authenticator.assertion(body["public_key"])},
        )
        assert verified.status_code == 200, verified.text
        return bound, verified.json()["proof_ref"]

    def human_gated_run(self, content: str = "def result():\n    return 'good'\n") -> str:
        request = GoalRequest.model_validate(
            {
                "goal": "Remote-requested bounded change.",
                "workspace_root": str(self.workspace),
                "scope_id": PROJECT,
                "changes": [{"path": "app.py", "content": content}],
                "acceptance_criteria": [
                    {"criterion_id": "good", "check": "FILE_CONTAINS", "target_path": "app.py",
                     "expected_text": "return 'good'"},
                    {"criterion_id": "scoped", "check": "CHANGED_PATHS_SCOPED"},
                ],
                "authorized": True,
                "human_gate": True,
            }
        )
        return str(asyncio.run(self.loop.execute(request)).run_id)


def build_slice(
    tmp_path: Path,
    *,
    settings: Settings | None = None,
    bind_host: str = "100.64.0.10",
    allowed_origins: tuple[str, ...] = (),
) -> Slice:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "README.md").write_text("Remote slice fixture project.\n", encoding="utf-8")
    gateway = InMemoryP6_19BStateGateway(tmp_path / "objects")
    loop = ProductLoopService(
        maker=FileDirectMaker(workspace, protected_roots=(Path("C:/PADOS"),)),
        validator=FileValidator(),
        state_gateway=gateway,
    )
    ports = InMemoryRemotePorts(gateway)
    authenticator = SoftwareAuthenticator()
    owner_value, viewer_value = secrets.token_hex(16), secrets.token_hex(16)
    registry = RemoteActorRegistry.from_mapping(
        {
            "actors": [
                {"actor_id": "owner", "credential_hash": hashlib.sha256(owner_value.encode()).hexdigest(),
                 "projects": [PROJECT], "may_approve": True, "may_control_jobs": True,
                 "presence_credentials": [authenticator.descriptor()]},
                {"actor_id": "viewer", "credential_hash": hashlib.sha256(viewer_value.encode()).hexdigest(),
                 "projects": [PROJECT]},
            ]
        }
    )

    def semantic(project_id: str) -> dict[str, Any] | None:
        if project_id != PROJECT:
            return None
        # Same shape as pados.execution.semantic_context: value + per-field provenance.
        sources = {
            "active_goal": "project_goals",
            "open_blockers": "project_gaps",
            "pending_decisions": "project_human_gates+memory_decisions",
            "recent_accepted_results": "state_proof_commits",
            "promoted_memory": "memory_authority_events",
            "evidence": "memory_artifacts",
        }
        semantic: dict[str, Any] = {
            name: {("item" if name == "active_goal" else "items"): ("remote slice" if name == "active_goal" else []),
                   "provenance": {"source": source, "project_id": project_id}}
            for name, source in sources.items()
        }
        semantic["scope_version"] = gateway.semantic_context(project_id)["scope_version"]
        return semantic

    service = RemoteOperationsService(
        registry=registry,
        reconstructor=RemoteContextReconstructor(
            semantic_source=semantic, workspace_resolver=lambda pid: workspace if pid == PROJECT else None
        ),
        staging=StagingArea(
            tmp_path / "staging",
            project_ceiling=lambda pid: DataClassification.CONFIDENTIAL if pid == PROJECT else None,
        ),
        jobs=RemoteJobController(NoControl(ports)),
        ledger=IntentLedger(tmp_path / "ledger" / "intents.jsonl"),
        ports=ports,
        step_up=StepUpService(InMemoryStepUpStore(), verifier()),
    )
    active = settings or hardened_settings()
    app = FastAPI()
    register_remote_routes(
        app,
        active,
        service=service,
        deployment=check_remote_deployment(active, remote_bind_host=bind_host),
        nodes=lambda: {"status": "COMPUTE_NODES_NOT_CONFIGURED", "nodes": [], "model_endpoints_exposed": False},
        allowed_origins=allowed_origins,
    )
    return Slice(
        client=TestClient(app),
        service=service,
        ports=ports,
        gateway=gateway,
        loop=loop,
        workspace=workspace,
        owner_headers={ACTOR_HEADER: "owner", PROOF_HEADER: owner_value},
        viewer_headers={ACTOR_HEADER: "viewer", PROOF_HEADER: viewer_value},
        authenticator=authenticator,
    )
