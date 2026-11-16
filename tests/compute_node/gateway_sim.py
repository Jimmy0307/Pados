# SPDX-License-Identifier: AGPL-3.0-only
"""A simulated enterprise AI gateway for tests and the simulated E2E (stdlib only; loopback only).

Its API is deliberately NOT OpenAI-shaped, so the adapter is proven to follow its contract rather than a convention:

  GET  /gw/health                -> {"ok": true}
  GET  /gw/catalog               -> {"items": [{"name", "revision", "state"}]}   (state READY or COLD)
  POST /gw/generate              -> {"result": {"text"}, "served_model", "usage": {"in", "out"}, "stop"}
       body {"model_name", "input": {"messages"}, "limits": {"max_tokens"}, "sampling": {"temperature"}}

Auth: header ``x-gateway-key`` must equal the value of the environment variable named by ``--key-env``.
Idempotency: a repeated ``Idempotency-Key`` returns the stored answer without a second side effect.

Control (tests only, no auth, loopback): ``POST /__sim/mode {"mode", "times", "delay"}`` and ``GET /__sim/stats``.
Modes: normal, unavailable (503), rate_limited (429 + Retry-After), not_found (404 on generate), cold (503 with error
code MODEL_COLD on generate), slow (sleeps ``delay`` s before answering), drop (processes the request, then closes
the connection mid-answer), forbid (403), wrong_model (answers with another model name). ``times`` limits how many
matching requests the mode applies to (0 = until changed).

Run: ``python tests/compute_node/gateway_sim.py --port 0 --key-env NAME [--tls-cert C --tls-key K]``; the first
stdout line is ``{"port": N}``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODELS = [
    {"name": "sim-small", "revision": "sha256:" + hashlib.sha256(b"sim-small").hexdigest(), "state": "READY"},
    {"name": "sim-large", "revision": "sha256:" + hashlib.sha256(b"sim-large").hexdigest(), "state": "COLD"},
]


class SimState:
    def __init__(self, key: str | None) -> None:
        self.key = key
        self.lock = threading.Lock()
        self.mode = "normal"
        self.times = 0
        self.delay = 0.0
        self.stats: dict[str, Any] = {"health": 0, "catalog": 0, "generate_requests": 0, "side_effects": 0,
                                      "replayed": 0, "unauthorized": 0, "keys": {}}
        self.answers: dict[str, dict[str, Any]] = {}

    def take(self, applies: bool) -> str:
        """The mode for this request (consumes one ``times`` unit when it applies)."""
        with self.lock:
            mode = self.mode
            if not applies or mode == "normal":
                return "normal"
            if self.times:
                self.times -= 1
                if self.times == 0:
                    self.mode = "normal"
            return mode


def make_handler(state: SimState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            return

        def _send(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> Any:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                return json.loads(raw) if raw else None
            except ValueError:
                return None

        def _authorized(self) -> bool:
            if state.key is None:
                return True
            if self.headers.get("x-gateway-key") != state.key:
                with state.lock:
                    state.stats["unauthorized"] += 1
                self._send(401, {"error": {"code": "UNAUTHENTICATED"}})
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802 - stdlib name
            if self.path == "/__sim/stats":
                with state.lock:
                    self._send(200, {**state.stats, "mode": state.mode})
                return
            if not self._authorized():
                return
            if self.path == "/gw/health":
                mode = state.take(True)
                with state.lock:
                    state.stats["health"] += 1
                if mode == "slow":
                    time.sleep(state.delay)
                if mode == "unavailable":
                    self._send(503, {"error": {"code": "MAINTENANCE"}})
                elif mode == "forbid":
                    self._send(403, {"error": {"code": "FORBIDDEN"}})
                else:
                    self._send(200, {"ok": True})
                return
            if self.path == "/gw/catalog":
                with state.lock:
                    state.stats["catalog"] += 1
                self._send(200, {"items": MODELS})
                return
            self._send(404, {"error": {"code": "NO_ROUTE"}})

        def do_POST(self) -> None:  # noqa: N802 - stdlib name
            body = self._body()
            if self.path == "/__sim/mode":
                with state.lock:
                    state.mode = str(body.get("mode", "normal"))
                    state.times = int(body.get("times", 0))
                    state.delay = float(body.get("delay", 0))
                self._send(200, {"mode": state.mode})
                return
            if self.path != "/gw/generate":
                self._send(404, {"error": {"code": "NO_ROUTE"}})
                return
            if not self._authorized():
                return
            self._generate(body)

        def _generate(self, body: Any) -> None:
            mode = state.take(True)
            key = self.headers.get("idempotency-key")
            with state.lock:
                state.stats["generate_requests"] += 1
                if key is not None:
                    state.stats["keys"][key] = state.stats["keys"].get(key, 0) + 1
                stored = state.answers.get(key) if key else None
            if stored is not None:
                with state.lock:
                    state.stats["replayed"] += 1
                self._send(200, stored)
                return
            if mode == "slow":
                time.sleep(state.delay)
            refusal = {"unavailable": (503, {"error": {"code": "MAINTENANCE"}}, {}),
                       "rate_limited": (429, {"error": {"code": "SLOW_DOWN"}}, {"retry-after": "1",
                                                                                 "x-quota-remaining": "0"}),
                       "not_found": (404, {"error": {"code": "UNKNOWN_MODEL"}}, {}),
                       "cold": (503, {"error": {"code": "MODEL_COLD"}}, {}),
                       "forbid": (403, {"error": {"code": "FORBIDDEN"}}, {})}.get(mode)
            if refusal is not None:
                self._send(*refusal)
                return
            model = body.get("model_name") if isinstance(body, dict) else None
            if model not in {m["name"] for m in MODELS}:
                self._send(404, {"error": {"code": "UNKNOWN_MODEL"}})
                return
            if model == "sim-large":
                self._send(503, {"error": {"code": "MODEL_COLD"}})
                return
            messages = (body.get("input") or {}).get("messages") or []
            text = " ".join(str(m.get("content", "")) for m in messages if isinstance(m, dict))
            answer = {"result": {"text": f"SIMULATED: {hashlib.sha256(text.encode()).hexdigest()[:12]}"},
                      "served_model": "sim-other" if mode == "wrong_model" else model,
                      "usage": {"in": len(text.split()), "out": 2}, "stop": "end"}
            with state.lock:
                state.stats["side_effects"] += 1
                if key:
                    state.answers[key] = answer
            if mode == "drop":
                # the request was processed (side effect recorded); the answer never arrives completely
                data = json.dumps(answer).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data) + 50))
                self.end_headers()
                self.wfile.write(data[:10])
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(2)
                return
            self._send(200, answer, {"x-quota-remaining": "99"})

    return Handler


def serve(port: int, key: str | None, cert: str | None = None, key_file: str | None = None) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(SimState(key)))
    server.daemon_threads = True
    if cert and key_file:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2  # never offer TLS 1.0/1.1, even in the test simulator
        context.load_cert_chain(cert, key_file)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--key-env", default=None)
    parser.add_argument("--tls-cert", default=None)
    parser.add_argument("--tls-key", default=None)
    args = parser.parse_args(argv)
    expected = os.environ.get(args.key_env) if args.key_env else None
    server = serve(args.port, expected, args.tls_cert, args.tls_key)
    print(json.dumps({"port": server.server_address[1]}), flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
