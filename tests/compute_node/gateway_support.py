# SPDX-License-Identifier: AGPL-3.0-only
"""Shared helpers for the simulated gateway: its contract, an in-process server and a test-only TLS certificate."""
from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import threading
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from tests.compute_node.gateway_sim import serve

KEY_ENV = "PADOS_SIM_GATEWAY_KEY"


def sim_key() -> str:
    return "sim-" + secrets.token_hex(12)


def sim_contract(base_url: str, **overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": "pados.compute-node.gateway-contract.v1",
        "provider_id": "sim-gateway",
        "display_name": "Simulated enterprise gateway",
        "base_url": base_url,
        "auth": {"scheme": "header", "auth_ref": f"secretref://env/{KEY_ENV}", "header_name": "x-gateway-key"},
        "health": {"route": {"method": "GET", "path": "/gw/health"}},
        "discovery": {"route": {"method": "GET", "path": "/gw/catalog"}, "models_path": "items", "id_field": "name",
                      "digest_field": "revision", "state_field": "state", "loaded_values": ["READY"]},
        "chat": {
            "route": {"method": "POST", "path": "/gw/generate"},
            "request_template": {"model_name": "{{model}}", "input": {"messages": "{{messages}}"},
                                 "limits": {"max_tokens": "{{max_output_tokens}}"},
                                 "sampling": {"temperature": "{{temperature}}"}},
            "text_path": "result.text", "model_path": "served_model", "finish_reason_path": "stop",
            "truncated_values": ["max_tokens"], "usage_prompt_path": "usage.in", "usage_completion_path": "usage.out",
            "error_code_path": "error.code",
            "error_codes": {"MODEL_COLD": "GATEWAY_MODEL_NOT_LOADED", "UNKNOWN_MODEL": "GATEWAY_MODEL_NOT_FOUND"},
        },
        "timeouts": {"connect_seconds": 1, "read_seconds": 2, "probe_seconds": 1},
        "retry": {"max_attempts": 3, "base_delay_seconds": 0.01, "max_delay_seconds": 0.05,
                  "max_retry_after_seconds": 2},
        "circuit": {"failure_threshold": 2, "open_seconds": 0.5},
        "rate_limit_headers": {"retry_after": "retry-after", "remaining": "x-quota-remaining"},
        "idempotency": {"header_name": "Idempotency-Key", "honoured": False},
        "max_context_tokens": 4096,
        "policy": {"max_classification": "INTERNAL", "execution_enabled": True},
    }
    for key, value in overrides.items():
        document[key] = value
    return document


@contextmanager
def running_sim(key: str | None, *, cert: str | None = None, key_file: str | None = None) -> Iterator[int]:
    server = serve(0, key, cert, key_file)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def sim_control(port: int, mode: str, *, times: int = 0, delay: float = 0.0, scheme: str = "http") -> None:
    request = urllib.request.Request(f"{scheme}://127.0.0.1:{port}/__sim/mode", method="POST",
                                     data=json.dumps({"mode": mode, "times": times, "delay": delay}).encode(),
                                     headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - loopback test server
        response.read()


def sim_stats(port: int) -> dict[str, Any]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/__sim/stats", timeout=5) as response:  # noqa: S310
        return dict(json.loads(response.read()))


def self_signed_certificate(directory: Path) -> tuple[Path, Path] | None:
    """A throwaway certificate for 127.0.0.1 made with the ``openssl`` binary (no Python dependency); None without it."""
    binary = shutil.which("openssl")
    if binary is None:
        return None
    cert, key = directory / "sim-cert.pem", directory / "sim-key.pem"
    result = subprocess.run(  # noqa: S603 - fixed arguments, test-only
        [binary, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(cert)],
        capture_output=True, timeout=60, check=False)
    return (cert, key) if result.returncode == 0 and cert.is_file() and key.is_file() else None
