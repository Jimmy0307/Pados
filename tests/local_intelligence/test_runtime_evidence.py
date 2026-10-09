"""Enterprise Local Runtime Adapter: external runtime-plane evidence -> the frozen live observation contracts.

The document is evidence only. A valid document lets ``bind_live`` work unchanged; anything malformed, stale,
oversized, credential-bearing or unauthenticated is refused or UNAVAILABLE; a lease never authorizes.
"""
from __future__ import annotations

import ast
import json
import pickle
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources"))
from capability_profile_support import DEVICE_ID, LICENSE, make_profile  # noqa: E402

from pados.capability.inference import (  # noqa: E402
    LocalProfileCandidate,
    MeasuredLocalCandidates,
    WorkerRouterBridge,
    registration_authorizes,
)
from pados.capability.seams import InferenceRequest  # noqa: E402
from pados.compute_node.manifest import NodeCapability  # noqa: E402
from pados.compute_node.registry import NodeRegistration  # noqa: E402
from pados.enterprise.classification import DataClassification  # noqa: E402
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary  # noqa: E402
from pados.local_intelligence import runtime_evidence as module  # noqa: E402
from pados.local_intelligence.runtime_evidence import (  # noqa: E402
    MAX_DOCUMENT_BYTES,
    RUNTIME_EVIDENCE_SCHEMA,
    EvidenceIntegrity,
    EvidenceKey,
    bind_external,
    ingest_runtime_evidence,
)
from pados.resources.capability_profile import (  # noqa: E402
    BindingPolicy,
    CandidateState,
    CapabilityFloor,
    CapabilityRequirement,
    MeasuredCapability,
    assess,
)
from pados.resources.models import ModelCatalog, profile_from_capability  # noqa: E402
from pados.worker.schemas import CapabilityId  # noqa: E402

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
POLICY = BindingPolicy()
PROFILE = make_profile()
REQUIREMENT = CapabilityRequirement(floors=(CapabilityFloor(capability=MeasuredCapability.REASONING,
                                                            min_success_lower_bound=0.4),))
KEY_MATERIAL = "k" * 48


def document(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schema_version": RUNTIME_EVIDENCE_SCHEMA,
        "plane_id": "enterprise-plane-1",
        "endpoint_scope": "PRIVATE_NETWORK",
        "observed_at": (NOW - timedelta(seconds=10)).isoformat(),
        "runtimes": [{
            "runtime_id": "rt-a", "runtime_family": PROFILE.binding.runtime_family,
            "version": PROFILE.binding.runtime_version, "state": "AVAILABLE",
            "artifacts": [{"runtime_ref": "plane-name-for-model",
                           "manifest_digest": PROFILE.artifact.manifest_digest}],
            "resident_refs": ["plane-name-for-model"]}],
        "device": {"device_profile_id": DEVICE_ID, "free_gpu_mib": 8000, "available_ram_mib": 30000,
                   "state": "AVAILABLE"},
        "leases": [{"lease_id": "lease-1", "runtime_ref": "plane-name-for-model",
                    "expires_at": (NOW + timedelta(minutes=5)).isoformat()}],
        "benchmark_evidence_refs": ["e" * 64],
    }
    body.update(overrides)
    return body


def raw(body: dict[str, Any]) -> bytes:
    return json.dumps(body).encode("utf-8")


def key_of(material: str) -> EvidenceKey:
    """The only way to obtain a key: the operator's ``secretref://`` resolved through the SecretBoundary."""
    boundary = SecretBoundary(EnvironmentSecretResolver({"PLANE_KEY": material}))
    return EvidenceKey.from_secret_ref(boundary, "secretref://env/PLANE_KEY")


KEY = key_of(KEY_MATERIAL)


def ingest(body: dict[str, Any] | bytes, **kwargs: Any) -> module.RuntimeEvidenceResult:
    """Ingest as the operator would: signed with the configured key unless the test says otherwise."""
    data = body if isinstance(body, bytes) else raw(body)
    if not kwargs:
        kwargs = {"key": KEY, "signature": KEY.sign(data)}
    return ingest_runtime_evidence(data, now=NOW, policy=POLICY, **kwargs)


def unavailable(result: module.RuntimeEvidenceResult) -> None:
    binding = bind_external(PROFILE, result, now=NOW, policy=POLICY)
    assert not binding.bindable and binding.runtime_ref is None
    assert binding.runtime_state is CandidateState.UNAVAILABLE and binding.authority == "NONE"


# --- valid evidence binds through the unchanged contract --------------------------------------------------------

def test_valid_document_binds_live_unchanged() -> None:
    result = ingest(document())
    assert result.accepted and result.integrity is EvidenceIntegrity.AUTHENTICATED
    assert result.authority == "NONE" and result.benchmark_evidence_refs == ("e" * 64,)
    binding = bind_external(PROFILE, result, now=NOW, policy=POLICY)
    assert binding.bindable and binding.runtime_ref == "plane-name-for-model"
    assert binding.headroom is not None and binding.headroom.model_resident
    assert assess(PROFILE, REQUIREMENT, runtime_state=binding.runtime_state, device_state=binding.device_state,
                  headroom=binding.headroom).eligible(REQUIREMENT)
    # The direct contract gives the same binding apart from the document-level reasons.
    direct = module.bind_live(PROFILE, result.runtimes, result.device, now=NOW, policy=POLICY)
    assert direct.runtime_ref == binding.runtime_ref and set(direct.reasons) <= set(binding.reasons)


def test_unauthenticated_evidence_is_unavailable_unless_explicitly_allowed_for_development() -> None:
    """Red-team F-3: a spoofed document naming the measured device and digest no longer binds by default."""
    data = raw(document())
    refused = ingest_runtime_evidence(data, now=NOW, policy=POLICY)  # no key configured: the default
    assert not refused.accepted and refused.reasons == ("EVIDENCE_UNAUTHENTICATED",)
    assert refused.integrity is EvidenceIntegrity.REFUSED
    unavailable(refused)
    assert not ingest_runtime_evidence(data, now=NOW, policy=POLICY, allow_unauthenticated="yes").accepted  # type: ignore[arg-type]
    opted_in = ingest_runtime_evidence(data, now=NOW, policy=POLICY, allow_unauthenticated=True)
    assert opted_in.accepted and opted_in.integrity is EvidenceIntegrity.UNAUTHENTICATED
    binding = bind_external(PROFILE, opted_in, now=NOW, policy=POLICY)
    assert binding.bindable and "EVIDENCE_UNAUTHENTICATED" in binding.reasons and binding.authority == "NONE"


def test_an_evidence_key_comes_only_from_a_secret_ref() -> None:
    """Red-team F-5: raw key bytes cannot be turned into a key outside the SecretBoundary."""
    with pytest.raises(TypeError, match="EVIDENCE_KEY_REQUIRES_SECRET_REF"):
        EvidenceKey(KEY_MATERIAL.encode())
    with pytest.raises(TypeError, match="EVIDENCE_KEY_REQUIRES_SECRET_REF"):
        EvidenceKey(KEY_MATERIAL.encode(), _token=object())


def test_authenticated_document_with_a_secretref_key() -> None:
    boundary = SecretBoundary(EnvironmentSecretResolver({"PLANE_KEY": KEY_MATERIAL}))
    key = EvidenceKey.from_secret_ref(boundary, "secretref://env/PLANE_KEY")
    data = raw(document())
    result = ingest(data, key=key, signature=key.sign(data))
    assert result.integrity is EvidenceIntegrity.AUTHENTICATED and not result.reasons
    assert bind_external(PROFILE, result, now=NOW, policy=POLICY).bindable
    assert "k" * 8 not in repr(key) and str(key) == "EvidenceKey([REDACTED])"
    with pytest.raises(TypeError):
        pickle.dumps(key)


def test_artifact_identity_is_the_digest_not_the_name() -> None:
    body = document()
    body["runtimes"][0]["artifacts"][0]["manifest_digest"] = "sha256:" + "f" * 64  # same name, other bytes
    binding = bind_external(PROFILE, ingest(body), now=NOW, policy=POLICY)
    assert "MODEL_ARTIFACT_NOT_PRESENT" in binding.reasons and not binding.bindable
    body["runtimes"][0]["artifacts"][0]["manifest_digest"] = "latest"  # a tag is not an identity
    result = ingest(body)
    assert not result.accepted and result.reasons == ("MANIFEST_DIGEST_INVALID",)


# --- refusal: every failure is closed codes and UNAVAILABLE -----------------------------------------------------

@pytest.mark.parametrize("data, code", [
    (b"{not json", "EVIDENCE_DOCUMENT_MALFORMED"),
    (b"[]", "EVIDENCE_DOCUMENT_MALFORMED"),
    (b"\xff\xfe", "EVIDENCE_DOCUMENT_MALFORMED"),
    (b'{"plane_id": "a", "plane_id": "b"}', "EVIDENCE_DOCUMENT_MALFORMED"),
    (raw(document(schema_version="v0")), "EVIDENCE_SCHEMA_UNSUPPORTED"),
    (raw(document(extra_field="x")), "EVIDENCE_DOCUMENT_INVALID"),
    (raw(document(observed_at="2026-10-09T12:00:00")), "EVIDENCE_TIMESTAMP_NOT_AWARE"),
    (raw(document(plane_id="../etc")), "EVIDENCE_LABEL_INVALID"),
    (raw(document(benchmark_evidence_refs=["not-a-digest"])), "BENCHMARK_EVIDENCE_REF_INVALID"),
    (raw(document(runtimes=[{"runtime_id": "x"}] * (module.MAX_RUNTIMES + 1))), "EVIDENCE_DOCUMENT_INVALID"),
])
def test_malformed_documents_are_refused_with_closed_codes(data: bytes, code: str) -> None:
    result = ingest(data)
    assert not result.accepted and result.reasons == (code,)
    assert result.runtimes == () and result.device is None
    unavailable(result)


def test_oversized_document_is_refused_before_parsing() -> None:
    result = ingest(b" " * (MAX_DOCUMENT_BYTES + 1))
    assert result.reasons == ("EVIDENCE_DOCUMENT_TOO_LARGE",) and result.document_digest is None
    unavailable(result)


@pytest.mark.parametrize("value, code", [
    ("https://user:pw@example.com/model", "EVIDENCE_URL_HAS_CREDENTIALS"),
    ("https://example.com/model?sig=abc", "EVIDENCE_URL_HAS_QUERY"),
    ("https://example.com/model#frag", "EVIDENCE_URL_HAS_QUERY"),
    ("user:pw@example.com/model", "EVIDENCE_URL_HAS_CREDENTIALS"),
    ("sk-" + "a" * 20, "EVIDENCE_CREDENTIAL_MATERIAL"),
    ("Bearer " + "b" * 20, "EVIDENCE_CREDENTIAL_MATERIAL"),
    ("secretref://env/PLANE_KEY", "EVIDENCE_CREDENTIAL_MATERIAL"),
    ("x" * 300, "EVIDENCE_STRING_TOO_LONG"),
])
def test_credentials_and_urls_with_queries_are_refused_and_never_echoed(value: str, code: str) -> None:
    body = document()
    body["runtimes"][0]["resident_refs"] = [value]
    result = ingest(body)
    assert result.reasons == (code,) and value not in result.model_dump_json()
    unavailable(result)
    keyed = document(**{value[:60]: "x"})  # credential material in a KEY is refused too
    assert not ingest(keyed).accepted


def test_unknown_states_are_unavailable_with_reasons() -> None:
    body = document()
    body["runtimes"][0]["state"] = "WARMING_UP"
    body["device"]["state"] = "MAYBE"
    result = ingest(body)
    assert result.accepted
    binding = bind_external(PROFILE, result, now=NOW, policy=POLICY)
    assert {"EXTERNAL_RUNTIME_STATE_UNKNOWN", "EXTERNAL_DEVICE_STATE_UNKNOWN"} <= set(binding.reasons)
    assert binding.runtime_state is CandidateState.UNAVAILABLE and binding.device_state is CandidateState.UNAVAILABLE


@pytest.mark.parametrize("age, code", [(timedelta(hours=1), "EVIDENCE_STALE"),
                                       (timedelta(minutes=-10), "EVIDENCE_STALE")])  # future is not fresh
def test_stale_and_future_documents_are_never_fresh(age: timedelta, code: str) -> None:
    result = ingest(document(observed_at=(NOW - age).isoformat()))
    assert code in result.reasons
    binding = bind_external(PROFILE, result, now=NOW, policy=POLICY)
    assert not binding.bindable
    assert {"RUNTIME_STATE_STALE", "DEVICE_STATE_STALE"} <= set(binding.reasons)


def test_another_device_or_unknown_identity_does_not_inherit_the_evidence() -> None:
    body = document()
    body["device"]["device_profile_id"] = "0" * 64
    assert "DEVICE_NOT_MEASURED" in bind_external(PROFILE, ingest(body), now=NOW, policy=POLICY).reasons
    body["device"]["device_profile_id"] = None
    assert not bind_external(PROFILE, ingest(body), now=NOW, policy=POLICY).bindable


def test_unauthenticated_documents_are_refused_when_a_key_is_configured() -> None:
    key = KEY
    data = raw(document())
    assert ingest(data, key=key).reasons == ("EVIDENCE_UNAUTHENTICATED",)
    assert ingest(data, key=key, signature="0" * 64).reasons == ("EVIDENCE_SIGNATURE_INVALID",)
    assert ingest(data, key=key, signature="not-hex").reasons == ("EVIDENCE_SIGNATURE_INVALID",)
    other = key_of("z" * 48)
    assert ingest(data, key=key, signature=other.sign(data)).reasons == ("EVIDENCE_SIGNATURE_INVALID",)
    tampered = data.replace(b"30000", b"90000")
    assert ingest(tampered, key=key, signature=key.sign(data)).reasons == ("EVIDENCE_SIGNATURE_INVALID",)
    for refused in (ingest(data, key=key), ingest(tampered, key=key, signature=key.sign(data))):
        unavailable(refused)
    with pytest.raises(ValueError, match="EVIDENCE_KEY_TOO_SHORT"):
        key_of("short")


def test_the_adapter_never_raises_into_the_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("defect")

    monkeypatch.setattr(module, "_ingest", broken)
    assert ingest(document()).reasons == ("EVIDENCE_ADAPTER_FAILED",)
    assert ingest_runtime_evidence("text", now=NOW).reasons == ("EVIDENCE_ADAPTER_FAILED",)  # type: ignore[arg-type]


# --- leases are evidence, never authorization -------------------------------------------------------------------

def test_expired_lease_is_a_reason_and_never_authorization() -> None:
    body = document(leases=[{"lease_id": "lease-old", "runtime_ref": "plane-name-for-model",
                             "expires_at": (NOW - timedelta(minutes=1)).isoformat()},
                            {"lease_id": "lease-other", "runtime_ref": "unknown-ref",
                             "expires_at": (NOW + timedelta(minutes=1)).isoformat()}])
    result = ingest(body)
    assert all(lease.authority == "NONE" for lease in result.leases)
    assert "EXTERNAL_LEASE_RUNTIME_REF_UNKNOWN" in result.leases[1].reasons
    binding = bind_external(PROFILE, result, now=NOW, policy=POLICY)
    assert "EXTERNAL_LEASE_EXPIRED" in binding.reasons and binding.authority == "NONE"


def _select(result: module.RuntimeEvidenceResult, registrations: list[NodeRegistration]) -> str | None:
    catalog = ModelCatalog([profile_from_capability(PROFILE, license=LICENSE)]).register_capability_profile(PROFILE)

    def candidates() -> list[LocalProfileCandidate]:
        return [LocalProfileCandidate.from_binding(p, bind_external(p, result, now=NOW, policy=POLICY))
                for p in catalog.capability_profiles()]

    source = MeasuredLocalCandidates(requirements={"MODEL_SUMMARY": REQUIREMENT}, candidates=candidates,
                                     authorization=registration_authorizes(lambda: registrations))
    bridge = WorkerRouterBridge(lambda: {}, {"MODEL_SUMMARY": CapabilityId.SUMMARIZE}, measured_local=source)
    request = InferenceRequest(project_id="p", stage_id="summary", capability_id="MODEL_SUMMARY", risk_level="LOW",
                               classification=DataClassification.INTERNAL, network_policy="LOCAL_ONLY",
                               grant_hash="g" * 64)
    return bridge.select(request).selected_worker_id


def _registration(*models: str) -> NodeRegistration:
    return NodeRegistration(node_id="lcn-" + "a" * 20, endpoint="http://127.0.0.1:8765",
                            auth_ref="secretref://env/PADOS_TEST_NODE_KEY", authorized=True,
                            allowed_capabilities=(NodeCapability.LOCAL_INFERENCE,),
                            max_classification=DataClassification.RESTRICTED, project_ids=("p",),
                            allowed_models=models)


def test_valid_lease_and_fresh_evidence_still_need_explicit_registration() -> None:
    boundary = SecretBoundary(EnvironmentSecretResolver({"PLANE_KEY": KEY_MATERIAL}))
    key = EvidenceKey.from_secret_ref(boundary, "secretref://env/PLANE_KEY")
    data = raw(document())
    result = ingest(data, key=key, signature=key.sign(data))  # authenticated, fresh, valid lease
    assert result.integrity is EvidenceIntegrity.AUTHENTICATED
    assert _select(result, []) is None  # no registration: nothing is routable
    assert _select(result, [_registration()]) is None  # empty allowed_models is DENY
    assert _select(result, [_registration(PROFILE.artifact.model_ref)]) is not None  # explicit operator grant
    refused = ingest(data, key=key)  # the same plane, unauthenticated: not routable even when registered
    assert _select(refused, [_registration(PROFILE.artifact.model_ref)]) is None


# --- static boundary ---------------------------------------------------------------------------------------------

def test_module_imports_no_router_network_or_authority() -> None:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    forbidden = ("pados.worker", "pados.capability", "pados.compute_node", "pados.state_proof", "pados.db",
                 "pados.learning", "socket", "http", "requests", "httpx", "subprocess", "urllib.request")
    assert not [name for name in names if name.startswith(forbidden)], names
    assert {"pados.resources.capability_profile", "pados.enterprise.secrets"} <= names
