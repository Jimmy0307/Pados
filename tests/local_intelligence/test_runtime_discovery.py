import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from li_fakes import NOW

from pados.compute_node.backends import BACKEND_KINDS
from pados.connectivity.requirements import NetworkRequirement
from pados.local_intelligence.probes import Basis, ProbeOutcome
from pados.local_intelligence.runtimes import (
    RUNTIME_PROBES,
    DiscoveryState,
    FetchError,
    RuntimeDescriptor,
    RuntimeEndpoint,
    RuntimeHealth,
    default_runtime_endpoints,
    describe_runtime,
)

TAGS = {"models": [
    {"name": "example-model:8b-q4_K_M", "size": 5_000_000_000, "digest": "a" * 64,
     "details": {"family": "examplefamily", "parameter_size": "8.2B", "quantization_level": "Q4_K_M", "format": "gguf"}},
    {"name": "../../etc/passwd", "size": 1},
    {"name": "bad name; rm -rf /"},
    {"name": "tiny:latest", "digest": "not-a-digest", "details": {"family": "<script>"}},
]}


def _fetch(responses):
    calls = []

    def fetch(path):
        calls.append(path)
        value = responses.get(path)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise FetchError("HTTP_404")
        return value if isinstance(value, bytes) else json.dumps(value).encode()

    fetch.calls = calls
    return fetch


def _ollama(responses, url="http://127.0.0.1:11434"):
    return describe_runtime(default_runtime_endpoints(url)[0], now=NOW, fetch=_fetch(responses))


def test_healthy_ollama_is_described_and_every_model_is_only_discovered():
    runtime = _ollama({"/api/version": {"version": "0.12.3"}, "/api/tags": TAGS,
                       "/api/ps": {"models": [{"name": "example-model:8b-q4_K_M", "size": 6_000_000_000,
                                               "size_vram": 5_500_000_000, "context_length": 8192}]}})
    assert runtime.health is RuntimeHealth.HEALTHY and runtime.version == "0.12.3" and runtime.local
    assert runtime.network_requirement is NetworkRequirement.LOCAL_ONLY and runtime.basis is Basis.PADOS_MEASURED
    assert [m.name for m in runtime.models] == ["example-model:8b-q4_K_M", "tiny:latest"]  # unsafe names dropped
    first, second = runtime.models
    assert (first.digest, first.quantization, first.parameter_size, first.family) == ("a" * 64, "Q4_K_M", "8.2B", "examplefamily")
    assert second.digest is None and second.family is None
    assert {m.state for m in runtime.models} == {DiscoveryState.DISCOVERED}
    assert runtime.loaded_models[0].size_vram_bytes == 5_500_000_000 and runtime.loaded_models[0].context_length == 8192
    assert runtime.structured_output.supported is True and runtime.structured_output.basis is Basis.UPSTREAM_FACT
    assert runtime.tool_use.supported is True and runtime.streaming.supported is True
    assert [p.probe_id for p in runtime.probes] == ["ollama.version", "ollama.tags", "ollama.ps"]
    assert all(p.output_sha256 for p in runtime.probes)


def test_old_runtime_versions_do_not_claim_newer_features():
    runtime = _ollama({"/api/version": {"version": "0.4.7"}, "/api/tags": {"models": []}, "/api/ps": {"models": []}})
    assert runtime.structured_output.supported is False and runtime.tool_use.supported is True


def test_absent_runtime_is_unreachable_not_an_error():
    runtime = _ollama({"/api/version": FetchError("CONNECTION_REFUSED")})
    assert runtime.health is RuntimeHealth.UNREACHABLE and runtime.failure_code == "VERSION_CONNECTION_REFUSED"
    assert not runtime.available and runtime.models == () and runtime.probes[0].outcome is ProbeOutcome.NOT_FOUND


@pytest.mark.parametrize("version_body", [b"<html>", {"version": "$(id)"}, {"nope": 1}, b"[1]"])
def test_a_non_runtime_answer_is_probe_failed(version_body):
    runtime = _ollama({"/api/version": version_body})
    assert runtime.health is RuntimeHealth.PROBE_FAILED and runtime.version is None
    assert runtime.probes[0].outcome is ProbeOutcome.INVALID_OUTPUT


def test_tags_failure_keeps_the_runtime_unhealthy():
    runtime = _ollama({"/api/version": {"version": "0.12.3"}, "/api/tags": FetchError("TIMEOUT")})
    assert runtime.health is RuntimeHealth.PROBE_FAILED and runtime.failure_code == "TAGS_TIMEOUT"


@pytest.mark.parametrize("url", ["http://192.168.1.5:11434", "https://ollama.example.com", "http://user:pw@127.0.0.1:11434",
                                 "http://127.0.0.1:11434/api?x=1"])
def test_discovery_never_probes_beyond_loopback_or_records_credentials(url):
    fetch = _fetch({})
    runtime = describe_runtime(RuntimeEndpoint(runtime_id="r1", runtime_type="ollama", endpoint=url), now=NOW, fetch=fetch)
    assert runtime.health is RuntimeHealth.NOT_PROBED and runtime.failure_code == "DISCOVERY_LOOPBACK_ONLY"
    assert fetch.calls == [] and "pw" not in runtime.endpoint and "?" not in runtime.endpoint


def test_openai_compatible_runtime():
    target = RuntimeEndpoint(runtime_id="llama-local", runtime_type="llama_cpp", endpoint="http://127.0.0.1:8080")
    runtime = describe_runtime(target, now=NOW, fetch=_fetch({"/v1/models": {"data": [{"id": "model.gguf"}, {"id": "../x"}]}}))
    assert runtime.health is RuntimeHealth.HEALTHY and [m.name for m in runtime.models] == ["model.gguf"]
    assert runtime.structured_output.basis is Basis.UNKNOWN


def test_runtime_kinds_are_exactly_compute_node_backend_kinds():
    assert set(RUNTIME_PROBES) <= set(BACKEND_KINDS)
    with pytest.raises(ValueError, match="RUNTIME_TYPE_NOT_A_COMPUTE_NODE_BACKEND_KIND"):
        RuntimeEndpoint(runtime_id="x1", runtime_type="vllm-private-router", endpoint="http://127.0.0.1:1")
    deterministic = describe_runtime(RuntimeEndpoint(runtime_id="det", runtime_type="deterministic",
                                                     endpoint="http://127.0.0.1:1"), now=NOW)
    assert deterministic.health is RuntimeHealth.NOT_PROBED


def test_descriptor_has_no_authorization_or_routing_fields():
    fields = set(RuntimeDescriptor.model_fields)
    assert not fields & {"authorized", "routable", "selected", "priority", "score", "grant"}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = {"/api/version": b'{"version":"0.12.3"}', "/api/tags": b'{"models":[]}', "/api/ps": b'{"models":[]}'}.get(self.path)
        self.send_response(200 if body else 404)
        self.send_header("Content-Length", str(len(body or b"")))
        self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, *args):
        return None


def test_live_loopback_transport_against_a_local_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        runtime = describe_runtime(default_runtime_endpoints(f"http://127.0.0.1:{server.server_port}")[0], now=NOW)
    finally:
        server.shutdown()
    assert runtime.health is RuntimeHealth.HEALTHY and runtime.version == "0.12.3" and runtime.models == ()
