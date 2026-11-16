# SPDX-License-Identifier: AGPL-3.0-only
"""Shared helpers for the Unified Identity & Session tests: a unique control value, a fake clock, a pairing harness."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

CONTROL_VALUE = "identity-suite-" + "control-value-0001"
TOKEN_HEADERS = {"X-PADOS-Control-Token": CONTROL_VALUE}
ORIGIN = "http://127.0.0.1:5173"


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@dataclass
class Harness:
    app: FastAPI
    clock: FakeClock
    store_dir: Path
    base_url: str = "http://testserver"
    clients: list[TestClient] = field(default_factory=list)

    def client(self, base_url: str | None = None) -> TestClient:
        made = TestClient(self.app, base_url=base_url or self.base_url)
        self.clients.append(made)
        return made

    def mint(self, client: TestClient, label: str | None = None) -> str:
        response = client.post("/identity/v1/pairing-codes", headers=TOKEN_HEADERS,
                               json={"label": label} if label else None)
        assert response.status_code == 200, response.text
        return str(response.json()["pairing_code"])

    def pair(self, client: TestClient, code: str, *, label: str | None = "Test browser",
             origin: str | None = ORIGIN) -> httpx.Response:
        headers = {"Origin": origin} if origin else {}
        body: dict[str, Any] = {"pairing_code": code}
        if label is not None:
            body["device_label"] = label
        return client.post("/identity/v1/pair", headers=headers, json=body)

    def paired(self, client: TestClient) -> dict[str, Any]:
        response = self.pair(client, self.mint(client))
        assert response.status_code == 200, response.text
        return dict(response.json())
