# SPDX-License-Identifier: AGPL-3.0-only
"""RC2 UX-2: `pados down` on Windows asks the API to shut down gracefully instead of force-killing it.

The launcher writes ``<home>/state/api.stop`` containing the instance id; the server it started sees its own id and
shuts uvicorn down. A stop file for another instance, or an empty one, is ignored.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from pados.install.serve import STOP_FILE, stop_requested, watch_stop_file


def test_only_this_instance_id_requests_a_stop(tmp_path: Path) -> None:
    path = tmp_path / STOP_FILE
    assert stop_requested(path, "instance-a") is False
    path.parent.mkdir(parents=True)
    path.write_text("instance-b", encoding="utf-8")
    assert stop_requested(path, "instance-a") is False
    path.write_text("", encoding="utf-8")
    assert stop_requested(path, "instance-a") is False
    path.write_text("instance-a\n", encoding="utf-8")
    assert stop_requested(path, "instance-a") is True


def test_the_watcher_asks_uvicorn_to_exit_gracefully(tmp_path: Path) -> None:
    server = SimpleNamespace(should_exit=False)
    path = tmp_path / STOP_FILE
    watcher = threading.Thread(target=watch_stop_file, args=(server, path, "instance-a"),
                               kwargs={"interval": 0.05}, daemon=True)
    watcher.start()
    time.sleep(0.2)
    assert server.should_exit is False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("instance-a", encoding="utf-8")
    watcher.join(timeout=2)
    assert server.should_exit is True and not watcher.is_alive()
    assert not path.exists()  # consumed, so a later instance never sees a stale request
