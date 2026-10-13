"""In-process node fixtures: the real node app served through FastAPI TestClient."""
from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pados.compute_node.config import NodeConfig
from pados.compute_node.registry import NodeRegistration, NodeRegistry
from pados.compute_node.service import NodeRuntime, create_node_app
from pados.enterprise import observability
from pados.enterprise.secrets import EnvironmentSecretResolver

CREDENTIAL_VALUE = "node-cred-" + "4f1c9a7e2b6d8053"
AUTH_REF = "secretref://env/PADOS_LCN_TEST_CREDENTIAL"
ENDPOINT = "http://127.0.0.1:8765"
PROJECT = "project-alpha"


def resolver(value: str | None = CREDENTIAL_VALUE) -> EnvironmentSecretResolver:
    return EnvironmentSecretResolver({"PADOS_LCN_TEST_CREDENTIAL": value} if value else {})


DEV_MODEL_ID = "deterministic/pados-deterministic-v1@1"


def node_config(tmp_path: Path, **updates: Any) -> NodeConfig:
    values: dict[str, Any] = {
        "node_name": "dev-node", "state_dir": tmp_path / "node-state", "credential_ref": AUTH_REF,
        "privacy_class": "RESTRICTED", "resource_class": "CPU_LIGHT",
        "capabilities": ["LOCAL_INFERENCE", "EMBEDDING", "CLASSIFICATION"],
        "limits": {"max_concurrent_jobs": 1, "max_queue_depth": 1, "default_timeout_seconds": 5,
                   "max_timeout_seconds": 30, "max_input_chars": 20_000, "max_batch_items": 8},
        "backends": [{"backend_id": "deterministic", "kind": "deterministic"}],
        "models": [{"backend_id": "deterministic", "backend_model": "pados-deterministic-v1",
                    "family": "pados-deterministic", "version": "1", "context_limit_tokens": 8192,
                    "tasks": ["LOCAL_INFERENCE", "EMBEDDING", "CLASSIFICATION"], "resource_class": "CPU_LIGHT",
                    "privacy_class": "RESTRICTED", "aliases": ["dev-model"]}],
    }
    values.update(updates)
    return NodeConfig.model_validate(values)


class RewritingClient:
    """Wraps a TestClient; ``rewrite`` may alter responses to emulate a malicious node."""

    def __init__(self, inner: TestClient, rewrite: Callable[[str, str, httpx.Response], httpx.Response] | None) -> None:
        self.inner = inner
        self.rewrite = rewrite

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        kwargs.pop("timeout", None)
        response = self.inner.request(method, path, **kwargs)
        return self.rewrite(method, path, response) if self.rewrite else response

    def close(self) -> None:
        pass


def json_response(response: httpx.Response, mutate: Callable[[dict[str, Any]], None]) -> httpx.Response:
    body = response.json()
    mutate(body)
    return httpx.Response(response.status_code, content=json.dumps(body).encode(), headers={"content-type": "application/json"})


@dataclass
class Node:
    runtime: NodeRuntime
    client: TestClient
    rewrite: Callable[[str, str, httpx.Response], httpx.Response] | None = None

    @property
    def node_id(self) -> str:
        return self.runtime.identity.node_id

    def http(self, endpoint: str) -> RewritingClient:
        assert endpoint == ENDPOINT
        return RewritingClient(self.client, self.rewrite)

    def registration(self, **updates: Any) -> NodeRegistration:
        values: dict[str, Any] = {"node_id": self.node_id, "node_name": "dev-node", "endpoint": ENDPOINT,
                                  "auth_ref": AUTH_REF, "authorized": True,
                                  "allowed_capabilities": ["LOCAL_INFERENCE", "EMBEDDING", "CLASSIFICATION"],
                                  "max_classification": "CONFIDENTIAL", "project_ids": [PROJECT],
                                  # Explicit model authorization: an empty allowlist authorizes no model.
                                  "allowed_models": [DEV_MODEL_ID]}
        values.update(updates)
        return NodeRegistration.model_validate(values)

    def registry(self, tmp_path: Path, *registrations: NodeRegistration) -> NodeRegistry:
        return NodeRegistry(state_dir=tmp_path / "pados-state", nodes=registrations or (self.registration(),))


def make_node(tmp_path: Path, **updates: Any) -> Node:
    runtime = NodeRuntime(node_config(tmp_path, **updates), resolver=resolver())
    return Node(runtime, TestClient(create_node_app(runtime), base_url=ENDPOINT))


@pytest.fixture
def node(tmp_path: Path) -> Iterator[Node]:
    created = make_node(tmp_path)
    yield created
    created.runtime.jobs.shutdown()


@pytest.fixture
def events() -> Iterator[list[dict[str, Any]]]:
    recorded: list[dict[str, Any]] = []
    remove = observability.add_sink(recorded.append)
    yield recorded
    remove()


def auth() -> dict[str, str]:
    return {"authorization": f"Bearer {CREDENTIAL_VALUE}"}
