from __future__ import annotations

import hashlib
from collections.abc import Iterator
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pados.api.app import create_app
from pados.config import Settings
from pados.control.projections import (
    END,
    START,
    render_command_center_canvas,
    render_project_markdown,
)
from pados.control.security import authorize_token, resolve_vault_path, token_hash

CONTROL_VALUE = "pados-test-control-" + "value"  # never the public shipped default

@pytest.fixture(autouse=True)
def _unique_control_hash(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The shipped default hash authenticates nothing: configure the hash of this module's test value."""
    from pados.config import get_settings
    monkeypatch.setenv("PADOS_CONTROL_API_TOKEN_HASH", hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()



def test_control_health_reports_localhost_only() -> None:
    client = TestClient(create_app())
    response = client.get("/control/health")
    assert response.status_code == 200
    assert response.json()["localhost_only"] is True


def test_control_auth_invalid_and_valid_token() -> None:
    settings = Settings()
    with pytest.raises(PermissionError, match="missing"):
        authorize_token(settings, None)
    with pytest.raises(PermissionError, match="invalid"):
        authorize_token(settings, "wrong")
    authorize_token(settings, CONTROL_VALUE)
    assert token_hash(CONTROL_VALUE) == hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper()


def test_control_protected_endpoint_rejects_missing_token_before_database() -> None:
    client = TestClient(create_app())
    response = client.get("/control/projects")
    assert response.status_code == 403


def test_projection_preserves_human_markdown_notes() -> None:
    existing = f"{START}\nold\n{END}\n\n## My Notes\nkeep this"
    rendered = render_project_markdown({"name": "Demo", "status": "active"}, existing)
    assert "Demo" in rendered
    assert "keep this" in rendered
    assert rendered.count(START) == 1
    assert rendered.count(END) == 1


def test_corrupt_markdown_markers_fail_closed() -> None:
    with pytest.raises(ValueError, match="projection conflict"):
        render_project_markdown({"name": "Demo"}, "missing managed markers")
    with pytest.raises(ValueError, match="projection conflict"):
        render_project_markdown({"name": "Demo"}, f"{START}\na\n{START}\nb\n{END}")


def test_canvas_stable_ids_and_manual_position_preservation() -> None:
    existing = {
        "nodes": [
            {"id": "pados-managed-today", "type": "text", "text": "Today", "x": 77, "y": 88},
            {"id": "user-note", "type": "text", "text": "Human", "x": 9, "y": 9},
        ],
        "edges": [{"id": "user-edge", "fromNode": "user-note", "toNode": "pados-managed-today"}],
    }
    rendered = render_command_center_canvas([{"project_id": "abc"}], existing)
    nodes = {node["id"]: node for node in rendered["nodes"]}
    assert nodes["pados-managed-today"]["x"] == 77
    assert "user-note" in nodes
    assert rendered["edges"] == existing["edges"]
    assert "pados-managed-project-abc" in nodes


def test_corrupt_canvas_fail_closed() -> None:
    with pytest.raises(ValueError, match="projection conflict"):
        render_command_center_canvas([], {"nodes": "not-list", "edges": []})


def test_path_traversal_blocked(tmp_path: Path) -> None:
    assert resolve_vault_path(tmp_path, "PADOS/00_COMMAND_CENTER.canvas").is_relative_to(tmp_path)
    with pytest.raises(PermissionError, match="path traversal"):
        resolve_vault_path(tmp_path, "../outside.md")


def test_plugin_manifest_and_source_contracts() -> None:
    root = Path("integrations/obsidian/pados-control-surface")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    source = (root / "src/main.ts").read_text(encoding="utf-8")
    assert manifest["isDesktopOnly"] is True
    assert "http://127.0.0.1:8088" in source
    assert "registerMarkdownCodeBlockProcessor" in source
    assert "localStorage" not in source
    assert "telemetry" not in source
