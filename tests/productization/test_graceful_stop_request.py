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


# R19-7: a `pados down` issued while the server is still starting must not be dropped. The launcher writes the
# request and waits up to 10 s before forcing termination; a server that deleted the file at startup lost the request
# and was force-killed instead of draining.


def test_a_stop_requested_before_the_watcher_starts_is_honoured(tmp_path: Path) -> None:
    from pados.install.serve import start_stop_watcher

    server = SimpleNamespace(should_exit=False)
    path = tmp_path / STOP_FILE
    path.parent.mkdir(parents=True)
    path.write_text("instance-a", encoding="utf-8")  # written while the process was still importing
    watcher = start_stop_watcher(server, tmp_path, "instance-a", interval=0.05)
    watcher.join(timeout=2)
    assert server.should_exit is True and not watcher.is_alive()
    assert not path.exists()


def test_a_stale_request_for_another_instance_is_left_alone_and_ignored(tmp_path: Path) -> None:
    from pados.install.serve import start_stop_watcher

    server = SimpleNamespace(should_exit=False)
    path = tmp_path / STOP_FILE
    path.parent.mkdir(parents=True)
    path.write_text("instance-old", encoding="utf-8")  # left by an interrupted `pados down` of an earlier instance
    watcher = start_stop_watcher(server, tmp_path, "instance-a", interval=0.05)
    time.sleep(0.2)
    assert server.should_exit is False and path.read_text(encoding="utf-8") == "instance-old"
    path.write_text("instance-a", encoding="utf-8")  # the launcher overwrites it with this instance's request
    watcher.join(timeout=2)
    assert server.should_exit is True and not path.exists()
