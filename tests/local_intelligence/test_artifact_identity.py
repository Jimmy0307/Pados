"""Stage B M2.1 regression: the pinned-artifact identity check and the failed-pull classification.

The M2 Windows run (archive m2-20261007T113855Z) reported ``PULLED_DIGEST_MISMATCH`` although the download had
failed with a TLS handshake timeout and no artifact was installed. These tests pin the corrected behaviour.
"""
import hashlib
import json

import pytest
from fake_ollama import MANIFEST_HEX, FakeOllama

from pados.local_intelligence.benchmark import (
    BenchmarkRefused,
    OllamaClient,
    PinnedArtifact,
    identity_refusal,
    local_manifest_path,
    normalize_digest,
    pull_and_verify,
    verify_present,
)

MANIFEST = f"sha256:{MANIFEST_HEX}"
MODEL_LAYER = "sha256:a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f"
LAYERS = (
    MODEL_LAYER,
    "sha256:ae370d884f108d16e7cc8fd5259ebc5773a0afa6e078b11f4ed7e39a27e0dfc4",
    "sha256:d18a5cc71b84bc4af394a31116bd3932b42241de70c77d2b76d69a314ec8aa12",
    "sha256:cff3f395ef3756ab63e58b0ad1b32bb6f802905cae1472e6a12034e4246fbbdb",
)
# The M1 registry manifest, byte for byte; its SHA-256 is the pinned manifest digest.
M1_MANIFEST_BYTES = (
    b'{"schemaVersion":2,"mediaType":"application/vnd.docker.distribution.manifest.v2+json","config":{"mediaType":'
    b'"application/vnd.docker.container.image.v1+json","digest":"sha256:05a61d37b08453e59290add468e3bb2f688e23a01e967fecb0e2fa'
    b'41218cea76","size":487},"layers":[{"mediaType":"application/vnd.ollama.image.model","digest":"sha256:a3de86cd1c132c82248'
    b'7ededd47a324c50491393e6565cd14bafa40d0b8e686f","size":5225374496},{"mediaType":"application/vnd.ollama.image.template",'
    b'"digest":"sha256:ae370d884f108d16e7cc8fd5259ebc5773a0afa6e078b11f4ed7e39a27e0dfc4","size":1723},{"mediaType":"applicati'
    b'on/vnd.ollama.image.license","digest":"sha256:d18a5cc71b84bc4af394a31116bd3932b42241de70c77d2b76d69a314ec8aa12","size":1'
    b'1338},{"mediaType":"application/vnd.ollama.image.params","digest":"sha256:cff3f395ef3756ab63e58b0ad1b32bb6f802905cae1472'
    b'e6a12034e4246fbbdb","size":120}]}'
)
PIN = PinnedArtifact("qwen3:8b", MANIFEST, LAYERS)
M2_ERROR = ('max retries exceeded: Get "https://example.r2.cloudflarestorage.com/ollama/docker/registry/v2/blobs/sha256/a3/'
            'a3de86cd1c132c822487ededd47a324c50491393e6565cd14bafa40d0b8e686f/data?X-Amz-Algorithm=AWS4-HMAC-SHA256&'
            'X-Amz-Signature=0123456789abcdef": net/http: TLS handshake timeout')


def test_the_fixture_is_the_m1_manifest():
    assert f"sha256:{hashlib.sha256(M1_MANIFEST_BYTES).hexdigest()}" == MANIFEST


def test_same_immutable_artifact_passes():
    assert identity_refusal(PIN, MANIFEST, M1_MANIFEST_BYTES) is None
    assert identity_refusal(PIN, MANIFEST_HEX, M1_MANIFEST_BYTES) is None  # Ollama's /api/tags reports bare hex


def test_manifest_digest_and_layer_digest_are_distinct_identities():
    assert identity_refusal(PIN, MODEL_LAYER, M1_MANIFEST_BYTES) == "MANIFEST_DIGEST_MISMATCH"
    assert identity_refusal(PinnedArtifact("qwen3:8b", MODEL_LAYER), MODEL_LAYER, None) is None  # only with its own pin
    assert identity_refusal(PinnedArtifact("qwen3:8b", MANIFEST, (MANIFEST,)), MANIFEST, M1_MANIFEST_BYTES) == "PIN_INVALID"


def test_wrong_manifest_refuses():
    assert identity_refusal(PIN, "sha256:" + "0" * 64, M1_MANIFEST_BYTES) == "MANIFEST_DIGEST_MISMATCH"
    assert identity_refusal(PIN, None, M1_MANIFEST_BYTES) == "MANIFEST_DIGEST_MISMATCH"
    tampered = M1_MANIFEST_BYTES.replace(b'"size":120', b'"size":121')
    assert identity_refusal(PIN, MANIFEST, tampered) == "LOCAL_MANIFEST_DIGEST_MISMATCH"


def test_wrong_layer_refuses_when_layer_pinning_is_required():
    other = json.loads(M1_MANIFEST_BYTES)
    other["layers"][0]["digest"] = "sha256:" + "1" * 64
    other_bytes = json.dumps(other, separators=(",", ":")).encode()
    other_manifest = f"sha256:{hashlib.sha256(other_bytes).hexdigest()}"
    pinned = PinnedArtifact("qwen3:8b", other_manifest, (MODEL_LAYER,))
    assert identity_refusal(pinned, other_manifest, other_bytes) == "LAYER_DIGEST_MISMATCH"
    assert identity_refusal(PinnedArtifact("qwen3:8b", other_manifest), other_manifest, other_bytes) is None  # not pinned
    assert identity_refusal(PIN, MANIFEST, None) == "LOCAL_MANIFEST_UNAVAILABLE"


def test_tag_equality_alone_is_insufficient():
    with FakeOllama(digest_hex="f" * 64, present=True) as fake:  # listed under the approved tag, other artifact
        result = verify_present(OllamaClient(fake.url), PIN, lambda: M1_MANIFEST_BYTES)
    assert result["identity_refusal"] == "MANIFEST_DIGEST_MISMATCH" and not result["digest_match"]
    with FakeOllama(present=True) as fake:  # right tag and runtime digest, but no local manifest to bind the layers
        result = verify_present(OllamaClient(fake.url), PIN, lambda: None)
    assert result["identity_refusal"] == "LOCAL_MANIFEST_UNAVAILABLE"
    with FakeOllama(present=True) as fake:
        result = verify_present(OllamaClient(fake.url), PIN, lambda: M1_MANIFEST_BYTES)
    assert result["identity_refusal"] is None and result["digest_match"] and result["local_manifest_sha256"] == MANIFEST_HEX


@pytest.mark.parametrize("value", [MANIFEST, MANIFEST_HEX, MANIFEST.upper(), f"  SHA256:{MANIFEST_HEX.upper()} "])
def test_digest_comparison_is_case_and_format_normalized(value):
    assert normalize_digest(value) == MANIFEST
    assert identity_refusal(PIN, value, M1_MANIFEST_BYTES) is None


@pytest.mark.parametrize("value", [None, "", MANIFEST_HEX[:-1], f"sha512:{MANIFEST_HEX}", f"sha256-{MANIFEST_HEX}",
                                   f"{MANIFEST_HEX}0", MANIFEST[:19], 42])
def test_malformed_digests_never_normalize(value):
    assert normalize_digest(value) is None
    assert identity_refusal(PinnedArtifact("qwen3:8b", str(value)), MANIFEST, None) == "PIN_INVALID"


def test_failed_pull_is_pull_failed_not_a_digest_mismatch():
    """Replay of M2: the pull stream ends in a transport error and the model never appears in /api/tags."""
    records: list[dict] = []
    with FakeOllama(pull_error=M2_ERROR) as fake, pytest.raises(BenchmarkRefused) as refused:
        pull_and_verify(OllamaClient(fake.url), PIN, records.append, lambda: None)
    assert refused.value.code == "PULL_FAILED"
    (entry,) = records
    assert entry["pull_outcome"] == "FAILED" and entry["identity_refusal"] == "MODEL_ABSENT"
    assert entry["status_sequence"] == ["pulling manifest", "pulling a3de86cd1c13", ""]
    assert "TLS handshake timeout" in entry["error"] and "X-Amz-Signature" not in entry["error"]
    assert "?<redacted>" in entry["error"]


def test_successful_pull_of_the_pinned_artifact_passes_and_wrong_one_refuses():
    with FakeOllama() as fake:
        result = pull_and_verify(OllamaClient(fake.url), PIN, [].append, lambda: M1_MANIFEST_BYTES)
    assert result["pull_outcome"] == "SUCCESS" and result["digest_match"]
    with FakeOllama(digest_hex="0" * 64) as fake, pytest.raises(BenchmarkRefused, match="PULLED_MANIFEST_DIGEST_MISMATCH"):
        pull_and_verify(OllamaClient(fake.url), PIN, [].append, lambda: M1_MANIFEST_BYTES)


def test_local_manifest_path(tmp_path):
    expected = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "qwen3" / "8b"
    assert local_manifest_path(tmp_path, "qwen3:8b") == expected
    assert local_manifest_path(tmp_path, "qwen3").name == "latest"
