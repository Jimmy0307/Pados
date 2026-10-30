# SPDX-License-Identifier: AGPL-3.0-only
"""Minimal, bounded MCP client over stdio (newline-delimited JSON-RPC 2.0).

Only what the capability loop needs: ``initialize`` / ``notifications/initialized`` and ``tools/call``.
The server command comes from operator configuration only (never from a request or a provider). It runs
without a shell, with a reduced environment (no inherited secrets), stdin/stdout pipes, stderr discarded,
one deadline per call, a per-message size cap, and the process is always terminated on close.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "pados-capability-loop", "version": "1"}
DEFAULT_MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_CHILD_ENV_KEYS = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
                   "TMPDIR", "XDG_RUNTIME_DIR")


class McpError(RuntimeError):
    """``code`` is one of: MCP_SPAWN_FAILED, MCP_TIMEOUT, MCP_CLOSED, MCP_MALFORMED, MCP_TOO_LARGE, MCP_RPC_ERROR."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class McpTransport(Protocol):
    def call_tool(self, name: str, arguments: Mapping[str, Any], *, timeout_seconds: float) -> dict[str, Any]: ...

    def close(self) -> None: ...


def child_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {key: os.environ[key] for key in _CHILD_ENV_KEYS if key in os.environ}
    env.update(extra or {})
    return env


class StdioMcpClient:
    def __init__(self, argv: Sequence[str], *, max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
                 env: Mapping[str, str] | None = None) -> None:
        if not argv or any(not isinstance(item, str) or not item or "\x00" in item for item in argv):
            raise McpError("MCP_SPAWN_FAILED")
        self._argv = list(argv)
        self._env = dict(env) if env is not None else child_environment()
        self._max = max_message_bytes
        self._process: subprocess.Popen[bytes] | None = None
        self._messages: queue.Queue[bytes | None] = queue.Queue()
        self._next_id = 0
        self._lock = threading.Lock()

    def start(self, *, timeout_seconds: float) -> dict[str, Any]:
        try:
            self._process = subprocess.Popen(  # noqa: S603 - operator-configured argv, no shell
                self._argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                shell=False, close_fds=True, env=self._env)
        except (OSError, ValueError) as exc:
            raise McpError("MCP_SPAWN_FAILED") from exc
        # The child is spawned; from here any failure must terminate and reap it (and let the reader thread
        # drain to EOF) before propagating, so a failed handshake never leaks a process or a pipe.
        try:
            threading.Thread(target=self._reader, daemon=True).start()
            result = self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                                  "clientInfo": CLIENT_INFO}, timeout_seconds)
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            return result
        except BaseException:
            self.close()
            raise

    def _reader(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._messages.put(None)
            return
        stream = process.stdout
        while True:
            line = stream.readline(self._max + 1)
            if not line:
                self._messages.put(None)
                return
            self._messages.put(line)
            if len(line) > self._max:
                return

    def _send(self, message: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise McpError("MCP_CLOSED")
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise McpError("MCP_CLOSED") from exc

    def _request(self, method: str, params: Mapping[str, Any], timeout_seconds: float) -> dict[str, Any]:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
            deadline = time.monotonic() + timeout_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise McpError("MCP_TIMEOUT")
                try:
                    line = self._messages.get(timeout=remaining)
                except queue.Empty as exc:
                    raise McpError("MCP_TIMEOUT") from exc
                if line is None:
                    raise McpError("MCP_CLOSED")
                if len(line) > self._max:
                    raise McpError("MCP_TOO_LARGE")
                try:
                    message = json.loads(line)
                except ValueError as exc:
                    raise McpError("MCP_MALFORMED") from exc
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise McpError("MCP_MALFORMED")
                if message.get("id") != request_id:
                    continue  # notifications and unrelated messages are ignored
                if "error" in message:
                    raise McpError("MCP_RPC_ERROR")
                result = message.get("result")
                if not isinstance(result, dict):
                    raise McpError("MCP_MALFORMED")
                return result

    def call_tool(self, name: str, arguments: Mapping[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        return self._request("tools/call", {"name": name, "arguments": dict(arguments)}, timeout_seconds)

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        self._process = None

    def __enter__(self) -> StdioMcpClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
