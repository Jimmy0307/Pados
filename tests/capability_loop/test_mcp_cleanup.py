# SPDX-License-Identifier: AGPL-3.0-only
"""Finding 7: a spawned MCP child must be terminated and reaped when initialize fails.

Once ``subprocess.Popen`` succeeds, any initialize failure (timeout, malformed response, RPC error, or the
child exiting) must still close pipes, terminate and reap the child, and leave no reader thread able to
leak. We spawn a real child that misbehaves during initialize and assert the process is dead afterward.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from pados.capability_loop import mcp_stdio
from pados.capability_loop.mcp_stdio import McpError, StdioMcpClient

# Each child reads the single initialize request line, then misbehaves. Keyed by expected McpError code.
_CHILDREN = {
    "MCP_TIMEOUT": "import sys,time; sys.stdin.readline(); time.sleep(10)",
    "MCP_MALFORMED": "import sys; sys.stdin.readline(); sys.stdout.write('not-json\\n'); sys.stdout.flush()",
    "MCP_RPC_ERROR": ("import sys,json; sys.stdin.readline(); "
                      "sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':1,"
                      "'error':{'code':-32000,'message':'nope'}})+'\\n'); sys.stdout.flush()"),
    "MCP_CLOSED": "pass",  # child exits immediately -> stdout EOF during initialize
}


@pytest.fixture
def recorded_processes(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen[bytes]]:
    created: list[subprocess.Popen[bytes]] = []
    real_popen = subprocess.Popen

    def recording_popen(*args: object, **kwargs: object) -> subprocess.Popen[bytes]:
        proc = real_popen(*args, **kwargs)  # type: ignore[arg-type]
        created.append(proc)
        return proc

    monkeypatch.setattr(mcp_stdio.subprocess, "Popen", recording_popen)
    return created


@pytest.mark.parametrize("expected_code", list(_CHILDREN))
def test_initialize_failure_terminates_and_reaps_child(
        expected_code: str, recorded_processes: list[subprocess.Popen[bytes]]) -> None:
    client = StdioMcpClient([sys.executable, "-c", _CHILDREN[expected_code]])
    with pytest.raises(McpError) as excinfo:
        client.start(timeout_seconds=0.5)
    assert excinfo.value.code == expected_code
    assert len(recorded_processes) == 1
    child = recorded_processes[0]
    assert child.poll() is not None  # child was terminated and reaped, not left running
    assert client._process is None  # the client released its handle


def test_bad_argv_never_spawns() -> None:
    # A malformed argv fails before any spawn, so there is nothing to clean up.
    with pytest.raises(McpError) as excinfo:
        StdioMcpClient([]).start(timeout_seconds=0.5)
    assert excinfo.value.code == "MCP_SPAWN_FAILED"
