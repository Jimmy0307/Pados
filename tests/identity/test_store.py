# SPDX-License-Identifier: AGPL-3.0-only
"""The identity store: owner-only atomic replace, fail-closed corruption, restart, settings validation (§3.3)."""
from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import structlog
from pydantic import ValidationError

from tests.identity.identity_support import ORIGIN, TOKEN_HEADERS, Harness
from tests.windows_links import create_link_or_skip


def _code(response_json: dict[str, object]) -> str:
    return str(response_json["error"]["code"])  # type: ignore[index]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only: on Windows os.chmod toggles only "
                    "the read-only flag; the store's protected DACL is tested in test_store_protection.py")
def test_store_files_are_owner_only(harness: Harness) -> None:
    harness.paired(harness.client())
    for name in ("sessions.json", "server.key"):
        assert stat.S_IMODE(os.stat(harness.store_dir / name).st_mode) == 0o600, name
    assert stat.S_IMODE(os.stat(harness.store_dir).st_mode) == 0o700


def test_a_save_replaces_the_file_and_never_writes_through_it(harness: Harness) -> None:
    client = harness.client()
    harness.paired(client)
    store = harness.store_dir / "sessions.json"
    linked = harness.store_dir.parent / "linked-copy.json"
    os.link(store, linked)
    before = linked.read_bytes()
    harness.paired(harness.client())
    assert linked.read_bytes() == before  # the old inode is untouched: the new document is a new file
    assert store.read_bytes() != before
    assert sorted(p.name for p in harness.store_dir.iterdir()) == ["server.key", "sessions.json"]  # no staged left
    document = json.loads(store.read_text(encoding="utf-8"))
    assert document["schema_version"] == "pados.identity.store.v1"


def test_the_server_key_is_created_once(harness: Harness) -> None:
    harness.paired(harness.client())
    key = (harness.store_dir / "server.key").read_bytes()
    assert len(key) == 32
    harness.paired(harness.client())
    assert (harness.store_dir / "server.key").read_bytes() == key


def test_restart_restores_sessions(make_harness: Callable[..., Harness]) -> None:
    first = make_harness()
    client = first.client()
    paired = first.paired(client)
    cookie = str(client.cookies.get("pados_session"))
    restarted = make_harness(store_dir=first.store_dir, clock=first.clock)
    browser = restarted.client()
    browser.cookies.set("pados_session", cookie)
    session = browser.get("/identity/v1/session").json()
    assert session["authenticated"] is True and session["csrf_token"] == paired["csrf_token"]
    assert session["device_id"] == paired["device_id"]


@pytest.mark.parametrize("content", [b"{not json", b"[]", b'{"schema_version": "pados.identity.store.v0"}',
                                     b'{"schema_version": "pados.identity.store.v1", "sessions": {"raw-id": {}}}',
                                     b"\xff\xfe"])
def test_corruption_fails_closed(harness: Harness, content: bytes) -> None:
    client = harness.client()
    paired = harness.paired(client)
    store = harness.store_dir / "sessions.json"
    store.write_bytes(content)
    with structlog.testing.capture_logs() as events:
        session = client.get("/identity/v1/session")
        control = client.get("/control/projects")
        write = client.post("/control/work-requests", json={},
                            headers={"Origin": ORIGIN, "X-PADOS-CSRF": paired["csrf_token"]})
        mint = client.post("/identity/v1/pairing-codes", headers=TOKEN_HEADERS)
    for response in (session, control, write, mint):
        assert response.status_code == 503 and _code(response.json()) == "IDENTITY_STORE_CORRUPT"
    assert store.read_bytes() == content  # never overwritten: the operator removes it (rollback)
    assert any(e.get("event") == "identity.store.corrupt" and e.get("error_code") == "IDENTITY_STORE_CORRUPT"
               for e in events)
    store.unlink()
    assert client.get("/identity/v1/session").json()["authenticated"] is False  # rollback: every session gone


def test_a_damaged_server_key_fails_closed(harness: Harness) -> None:
    client = harness.client()
    harness.paired(client)
    (harness.store_dir / "server.key").write_bytes(b"short")
    response = client.get("/identity/v1/session")
    assert response.status_code == 503 and _code(response.json()) == "IDENTITY_STORE_CORRUPT"


def test_a_linked_store_document_is_refused(harness: Harness, tmp_path: Path) -> None:
    harness.paired(harness.client())
    store = harness.store_dir / "sessions.json"
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_bytes(store.read_bytes())
    store.unlink()
    create_link_or_skip(elsewhere, store, directory=False)
    response = harness.client().post("/identity/v1/pairing-codes", headers=TOKEN_HEADERS)
    assert response.status_code == 503 and _code(response.json()) == "IDENTITY_STORE_CORRUPT"


def test_identity_settings_are_validated() -> None:
    from pados.config import IDENTITY_ALLOWED_ORIGINS_DEFAULT, Settings

    defaults = Settings()
    assert defaults.identity_sessions_enabled is False
    assert defaults.identity_store_dir == "runtime/local/identity"
    assert (defaults.identity_session_ttl_seconds, defaults.identity_idle_timeout_seconds) == (86400, 7200)
    assert defaults.identity_cookie_secure == "auto"
    assert defaults.identity_origins == tuple(IDENTITY_ALLOWED_ORIGINS_DEFAULT.split(","))
    assert Settings(PADOS_IDENTITY_COOKIE_SECURE="ALWAYS").identity_cookie_secure == "always"
    assert Settings(PADOS_IDENTITY_ALLOWED_ORIGINS=" https://pados.example:8443 ,http://[::1]:5173").identity_origins \
        == ("https://pados.example:8443", "http://[::1]:5173")
    for bad in ("*", "http://127.0.0.1:5173/", "http://127.0.0.1:5173/path", "HTTP://LOCALHOST:5173",
                "ftp://localhost", "http://user@localhost:5173", "null"):
        with pytest.raises(ValidationError):
            Settings(PADOS_IDENTITY_ALLOWED_ORIGINS=bad)
    with pytest.raises(ValidationError):
        Settings(PADOS_IDENTITY_COOKIE_SECURE="sometimes")
    with pytest.raises(ValidationError):
        Settings(PADOS_IDENTITY_SESSION_TTL_SECONDS=600, PADOS_IDENTITY_IDLE_TIMEOUT_SECONDS=900)
    with pytest.raises(ValidationError):
        Settings(PADOS_IDENTITY_SESSION_TTL_SECONDS=10)
