"""Fresh approval step-up: human presence (WebAuthn UV) and single-use, fully bound proofs."""
from __future__ import annotations

import ast
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from pados.remote import presence as webauthn
from pados.remote.auth import RemoteActorRegistry
from pados.remote.config import RemoteConfig
from pados.remote.step_up import (
    ApprovalTarget,
    InMemoryStepUpStore,
    StepUpAction,
    StepUpBinding,
    StepUpError,
    StepUpService,
)
from tests.remote.presence_support import ORIGIN, SoftwareAuthenticator, cbor, verifier

ROOT = Path(__file__).resolve().parents[2]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def _target(**changes: object) -> ApprovalTarget:
    base = ApprovalTarget(
        approval_ref="appr-1", kind="RESULT_CANDIDATE", project_id="proj-a", task_id="task:1",
        goal_id=None, candidate_id="candidate:abc", candidate_digest="d" * 64,
        classification="INTERNAL", expected_scope_version=3, target_run_id="candidate:abc",
    )
    return replace(base, **changes)  # type: ignore[arg-type]


@pytest.fixture
def env() -> tuple[StepUpService, SoftwareAuthenticator, Clock, str]:
    clock = Clock()
    service = StepUpService(InMemoryStepUpStore(), verifier(), clock=clock)
    authenticator = SoftwareAuthenticator()
    session = service.resolve_session("owner", service.open_session("owner")["session_handle"])
    return service, authenticator, clock, session


def _binding(session: str, *, actor: str = "owner", action: StepUpAction = StepUpAction.APPROVE,
             **target: object) -> StepUpBinding:
    return StepUpBinding(actor_id=actor, session_id=session, action=action, target=_target(**target))


def _creds(authenticator: SoftwareAuthenticator) -> tuple[webauthn.PresenceCredential, ...]:
    return (webauthn.parse_presence_credential(authenticator.descriptor()),)


def _proof(service: StepUpService, authenticator: SoftwareAuthenticator, binding: StepUpBinding) -> str:
    challenge = service.issue_challenge(binding, _creds(authenticator))
    verified = service.verify_presence(
        challenge_id=challenge["challenge_id"], actor_id=binding.actor_id, session_id=binding.session_id,
        approval_ref=binding.target.approval_ref, assertion=authenticator.assertion(challenge["public_key"]),
        credentials=_creds(authenticator),
    )
    return str(verified["proof_ref"])


def test_challenge_binds_every_authoritative_field(env) -> None:
    service, authenticator, _clock, session = env
    binding = _binding(session)
    challenge = service.issue_challenge(binding, _creds(authenticator))
    assert challenge["binding_digest"] == binding.digest
    shown = challenge["binding"]
    assert shown["candidate_id"] == "candidate:abc" and shown["candidate_digest"] == "d" * 64
    assert shown["expected_scope_version"] == 3 and shown["classification"] == "INTERNAL"
    assert challenge["public_key"]["userVerification"] == "required"
    assert challenge["public_key"]["rpId"] == "localhost"
    assert set(binding.to_dict()) == {"actor_id", "session_id", "action", "target", "protocol_version",
                                       "schema_version"}
    assert binding.to_dict()["schema_version"] == "pados.approval-step-up.v1"


def test_valid_presence_mints_single_use_proof_and_replay_is_denied(env) -> None:
    service, authenticator, _clock, session = env
    binding = _binding(session)
    proof = _proof(service, authenticator, binding)
    consumed = service.consume(proof, binding)
    assert consumed.status == "CONSUMED" and consumed.presence_method == "WEBAUTHN_USER_VERIFICATION"
    with pytest.raises(StepUpError, match="APPROVAL_PROOF_REPLAYED"):
        service.consume(proof, binding)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ({"actor": "intruder"}, "APPROVAL_PROOF_ACTOR_MISMATCH"),
        ({"session": "rs-" + "0" * 32}, "APPROVAL_PROOF_SESSION_MISMATCH"),
        ({"task_id": "task:other"}, "APPROVAL_PROOF_TASK_MISMATCH"),
        ({"project_id": "proj-b"}, "APPROVAL_PROOF_PROJECT_MISMATCH"),
        ({"candidate_id": "candidate:other"}, "APPROVAL_PROOF_CANDIDATE_MISMATCH"),
        ({"candidate_digest": "e" * 64}, "APPROVAL_PROOF_CANDIDATE_DIGEST_MISMATCH"),
        ({"action": StepUpAction.REJECT}, "APPROVAL_PROOF_ACTION_MISMATCH"),
        ({"expected_scope_version": 4}, "APPROVAL_PROOF_SCOPE_VERSION_MISMATCH"),
        ({"classification": "CONFIDENTIAL"}, "APPROVAL_PROOF_CLASSIFICATION_MISMATCH"),
        ({"approval_ref": "appr-2"}, "APPROVAL_PROOF_TARGET_MISMATCH"),
        ({"goal_id": "goal-x"}, "APPROVAL_PROOF_GOAL_MISMATCH"),
    ],
)
def test_proof_is_denied_for_any_binding_drift(env, mutation: dict[str, object], code: str) -> None:
    service, authenticator, _clock, session = env
    proof = _proof(service, authenticator, _binding(session))
    actor = str(mutation.pop("actor", "owner"))
    other_session = str(mutation.pop("session", session))
    action = mutation.pop("action", StepUpAction.APPROVE)
    drifted = _binding(other_session, actor=actor, action=action, **mutation)  # type: ignore[arg-type]
    with pytest.raises(StepUpError, match=code):
        service.consume(proof, drifted)
    # The legitimate binding can still use it exactly once (drift attempts never consume).
    assert service.consume(proof, _binding(session)).status == "CONSUMED"


def test_expired_challenge_and_expired_proof_are_denied(env) -> None:
    service, authenticator, clock, session = env
    binding = _binding(session)
    challenge = service.issue_challenge(binding, _creds(authenticator))
    clock.now += timedelta(seconds=121)
    with pytest.raises(StepUpError, match="APPROVAL_CHALLENGE_EXPIRED"):
        service.verify_presence(
            challenge_id=challenge["challenge_id"], actor_id="owner", session_id=session, approval_ref="appr-1",
            assertion=authenticator.assertion(challenge["public_key"]), credentials=_creds(authenticator),
        )
    proof = _proof(service, authenticator, binding)
    clock.now += timedelta(seconds=61)
    with pytest.raises(StepUpError, match="APPROVAL_PROOF_EXPIRED"):
        service.consume(proof, binding)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"user_verified": False}, "APPROVAL_PRESENCE_USER_VERIFICATION_REQUIRED"),
        ({"user_present": False}, "APPROVAL_PRESENCE_USER_PRESENCE_REQUIRED"),
        ({"origin": "https://evil.example"}, "APPROVAL_PRESENCE_ORIGIN_NOT_ALLOWED"),
        ({"rp_id": "evil.example"}, "APPROVAL_PRESENCE_RP_ID_MISMATCH"),
        ({"challenge": webauthn.b64url_encode(b"x" * 32)}, "APPROVAL_PRESENCE_CHALLENGE_MISMATCH"),
        ({"tamper_signature": True}, "APPROVAL_PRESENCE_SIGNATURE_INVALID"),
    ],
)
def test_presence_failures_are_denied_and_burn_the_challenge(env, kwargs: dict[str, object], code: str) -> None:
    service, authenticator, _clock, session = env
    challenge = service.issue_challenge(_binding(session), _creds(authenticator))
    with pytest.raises(StepUpError, match=code):
        service.verify_presence(
            challenge_id=challenge["challenge_id"], actor_id="owner", session_id=session, approval_ref="appr-1",
            assertion=authenticator.assertion(challenge["public_key"], **kwargs),  # type: ignore[arg-type]
            credentials=_creds(authenticator),
        )
    with pytest.raises(StepUpError, match="APPROVAL_CHALLENGE_ALREADY_USED"):
        service.verify_presence(
            challenge_id=challenge["challenge_id"], actor_id="owner", session_id=session, approval_ref="appr-1",
            assertion=authenticator.assertion(challenge["public_key"]), credentials=_creds(authenticator),
        )


def test_unregistered_authenticator_and_wrong_session_are_denied(env) -> None:
    service, authenticator, _clock, session = env
    challenge = service.issue_challenge(_binding(session), _creds(authenticator))
    stranger = SoftwareAuthenticator()
    with pytest.raises(StepUpError, match="APPROVAL_CHALLENGE_SESSION_MISMATCH"):
        service.verify_presence(
            challenge_id=challenge["challenge_id"], actor_id="owner", session_id="rs-" + "1" * 32,
            approval_ref="appr-1", assertion=authenticator.assertion(challenge["public_key"]),
            credentials=_creds(authenticator),
        )
    with pytest.raises(StepUpError, match="APPROVAL_PRESENCE_CREDENTIAL_NOT_REGISTERED"):
        service.verify_presence(
            challenge_id=challenge["challenge_id"], actor_id="owner", session_id=session, approval_ref="appr-1",
            assertion=stranger.assertion(challenge["public_key"]), credentials=_creds(authenticator),
        )


def test_sign_count_regression_is_refused(env) -> None:
    service, authenticator, _clock, session = env
    _proof(service, authenticator, _binding(session))
    authenticator.sign_count = 0  # a cloned authenticator restarts its counter
    challenge = service.issue_challenge(_binding(session), _creds(authenticator))
    with pytest.raises(StepUpError, match="APPROVAL_PRESENCE_SIGN_COUNT_REGRESSION"):
        service.verify_presence(
            challenge_id=challenge["challenge_id"], actor_id="owner", session_id=session, approval_ref="appr-1",
            assertion=authenticator.assertion(challenge["public_key"]), credentials=_creds(authenticator),
        )


def test_sessions_are_actor_bound_and_expire(env) -> None:
    service, _authenticator, clock, _session = env
    handle = service.open_session("owner")["session_handle"]
    with pytest.raises(StepUpError, match="REMOTE_SESSION_INVALID"):
        service.resolve_session("intruder", handle)
    with pytest.raises(StepUpError, match="REMOTE_SESSION_INVALID"):
        service.resolve_session("owner", "x" * 43)
    clock.now += timedelta(hours=9)
    with pytest.raises(StepUpError, match="REMOTE_SESSION_EXPIRED"):
        service.resolve_session("owner", handle)


def test_unconfigured_presence_and_unenrolled_actor_fail_closed() -> None:
    service = StepUpService(InMemoryStepUpStore(), None)
    session = service.resolve_session("owner", service.open_session("owner")["session_handle"])
    with pytest.raises(StepUpError, match="APPROVAL_PRESENCE_NOT_CONFIGURED"):
        service.issue_challenge(_binding(session), ())
    configured = StepUpService(InMemoryStepUpStore(), verifier())
    session = configured.resolve_session("owner", configured.open_session("owner")["session_handle"])
    with pytest.raises(StepUpError, match="APPROVAL_PRESENCE_CREDENTIAL_NOT_ENROLLED"):
        configured.issue_challenge(_binding(session), ())


def test_default_remote_config_has_no_presence_verifier(tmp_path: Path) -> None:
    from pados.remote.app import presence_verifier

    config = RemoteConfig.from_mapping({"bind_host": "100.64.0.10", "staging_root": str(tmp_path / "s"),
                                        "ledger_path": str(tmp_path / "l.jsonl"), "actors": []})
    assert presence_verifier(config) is None
    with pytest.raises(ValueError, match="REMOTE_CONFIG_PRESENCE_ORIGIN_NOT_IN_RP_ID"):
        RemoteConfig.from_mapping({"bind_host": "100.64.0.10", "staging_root": "s", "ledger_path": "l",
                                   "actors": [], "presence": {"rp_id": "pados.example",
                                                              "origins": ["https://evil.example"]}})


def test_enrollment_descriptor_registers_on_existing_actor_registry() -> None:
    authenticator = SoftwareAuthenticator()
    rp = verifier()
    challenge = b"c" * 32
    options = rp.registration_options(challenge, actor_id="owner", user_handle=webauthn.user_handle_for("owner"))
    descriptor = rp.enrollment_descriptor(authenticator.registration(options), challenge=challenge, label="phone")
    assert descriptor["status"] == "DESCRIPTOR_ONLY_NOT_ENROLLED_CONFIRM_DEVICE_OUT_OF_BAND"
    assert descriptor["attestation_verified"] is False
    row = {key: descriptor[key] for key in ("credential_id", "algorithm", "public_key_cose", "label")}
    registry = RemoteActorRegistry.from_mapping({"actors": [{
        "actor_id": "owner", "credential_hash": "A" * 64, "projects": ["p"], "may_approve": True,
        "presence_credentials": [row]}]})
    actor = registry.get("owner")
    assert actor is not None and actor.presence_credentials[0].credential_id == row["credential_id"]
    with pytest.raises(webauthn.PresenceError, match="CHALLENGE_MISMATCH"):
        rp.enrollment_descriptor(authenticator.registration(options), challenge=b"d" * 32, label="phone")
    with pytest.raises(ValueError, match="REMOTE_PRESENCE_CREDENTIAL_DUPLICATE"):
        RemoteActorRegistry.from_mapping({"actors": [
            {"actor_id": "a", "credential_hash": "A" * 64, "presence_credentials": [row]},
            {"actor_id": "b", "credential_hash": "B" * 64, "presence_credentials": [row]}]})


def test_presence_credential_rows_are_closed_and_public_only() -> None:
    authenticator = SoftwareAuthenticator()
    with pytest.raises(ValueError, match="REMOTE_PRESENCE_CREDENTIAL_INVALID"):
        webauthn.parse_presence_credential({**authenticator.descriptor(), "private_key": "x"})
    bad_key = webauthn.b64url_encode(cbor({1: 2, 3: -7, -1: 1, -2: b"\x01" * 32, -3: b"\x02" * 32}))
    with pytest.raises(ValueError, match="REMOTE_PRESENCE_CREDENTIAL_INVALID"):
        webauthn.parse_presence_credential({**authenticator.descriptor(), "public_key_cose": bad_key})
    with pytest.raises(ValueError, match="ALGORITHM_UNSUPPORTED"):
        webauthn.parse_presence_credential({**authenticator.descriptor(), "algorithm": -8})


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl CLI not available")
def test_es256_and_rs256_verify_against_openssl(tmp_path: Path) -> None:
    message = tmp_path / "m"
    message.write_bytes(b"pados presence interop")
    ec, rsa = tmp_path / "ec.pem", tmp_path / "rsa.pem"
    run = lambda *args: subprocess.run(args, check=True, capture_output=True)  # noqa: E731
    run("openssl", "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(ec))
    run("openssl", "dgst", "-sha256", "-sign", str(ec), "-out", str(tmp_path / "es"), str(message))
    run("openssl", "ec", "-in", str(ec), "-pubout", "-outform", "DER", "-out", str(tmp_path / "pub"))
    point = (tmp_path / "pub").read_bytes()[-64:]
    x, y = int.from_bytes(point[:32], "big"), int.from_bytes(point[32:], "big")
    signature = (tmp_path / "es").read_bytes()
    assert webauthn.verify_es256(x, y, message.read_bytes(), signature)
    assert not webauthn.verify_es256(x, y, message.read_bytes() + b"!", signature)
    run("openssl", "genrsa", "-out", str(rsa), "2048")
    run("openssl", "dgst", "-sha256", "-sign", str(rsa), "-out", str(tmp_path / "rs"), str(message))
    modulus = run("openssl", "rsa", "-in", str(rsa), "-noout", "-modulus").stdout.decode().split("=")[1]
    n = int(modulus.strip(), 16)
    assert webauthn.verify_rs256(n, 65537, message.read_bytes(), (tmp_path / "rs").read_bytes())
    assert not webauthn.verify_rs256(n, 65537, message.read_bytes() + b"!", (tmp_path / "rs").read_bytes())


def test_no_presence_bypass_exists_in_production_source() -> None:
    """The only verifier in src is WebAuthn with UV required; test signers live under tests/."""
    source_root = ROOT / "src" / "pados"
    offenders = []
    for path in source_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8-sig")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and "Verifier" in node.name and node.name != "WebAuthnPresenceVerifier":
                if "presence" in text.lower() and "step_up" in text.lower():
                    offenders.append(f"{path.relative_to(ROOT)}:{node.name}")
        if "SoftwareAuthenticator" in text or "TEST_ONLY_PRESENCE" in text.upper():
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
    assert ORIGIN.startswith("http://localhost")


def test_signed_challenge_commits_to_the_binding_and_evidence_is_reverifiable(env) -> None:
    import hashlib

    from pados.remote.step_up import signed_challenge

    service, authenticator, _clock, session = env
    binding = _binding(session)
    challenge = service.issue_challenge(binding, _creds(authenticator))
    record = service.store.get_challenge(challenge["challenge_id"])
    assert record is not None
    expected = hashlib.sha256(webauthn.b64url_decode(record.nonce) + bytes.fromhex(binding.digest)).digest()
    assert challenge["public_key"]["challenge"] == webauthn.b64url_encode(expected)
    assert signed_challenge(record.nonce, binding.digest) == expected
    verified = service.verify_presence(
        challenge_id=challenge["challenge_id"], actor_id="owner", session_id=session, approval_ref="appr-1",
        assertion=authenticator.assertion(challenge["public_key"]), credentials=_creds(authenticator))
    stored = service.store.get_challenge(challenge["challenge_id"])
    assert stored is not None and stored.assertion_evidence is not None
    assert verified["assertion_digest"] == service_digest(stored.assertion_evidence)
    # Anyone holding the enrolled public key can re-verify the stored assertion for this exact binding.
    evidence = stored.assertion_evidence
    reverified = verifier().verify_assertion(
        {key: evidence[key] for key in ("credential_id", "client_data_json", "authenticator_data", "signature")},
        challenge=signed_challenge(stored.nonce, stored.binding_digest), credentials=_creds(authenticator))
    assert reverified.user_verified
    with pytest.raises(webauthn.PresenceError, match="CHALLENGE_MISMATCH"):
        verifier().verify_assertion(
            {key: evidence[key] for key in ("credential_id", "client_data_json", "authenticator_data", "signature")},
            challenge=signed_challenge(stored.nonce, "0" * 64), credentials=_creds(authenticator))


def service_digest(evidence: dict[str, str]) -> str:
    from pados.remote.step_up import assertion_digest

    return assertion_digest(evidence)


def test_active_challenges_and_sessions_are_capped(env) -> None:
    from pados.remote.step_up import MAX_ACTIVE_CHALLENGES_PER_ACTOR, MAX_ACTIVE_SESSIONS_PER_ACTOR

    service, authenticator, _clock, session = env
    for _ in range(MAX_ACTIVE_CHALLENGES_PER_ACTOR):
        service.issue_challenge(_binding(session), _creds(authenticator))
    with pytest.raises(StepUpError, match="APPROVAL_CHALLENGE_LIMIT_REACHED"):
        service.issue_challenge(_binding(session), _creds(authenticator))
    for _ in range(MAX_ACTIVE_SESSIONS_PER_ACTOR - 1):
        service.open_session("owner")
    with pytest.raises(StepUpError, match="REMOTE_SESSION_LIMIT_REACHED"):
        service.open_session("owner")


def test_enrollment_challenge_is_server_held_single_use_and_actor_bound(env) -> None:
    service, authenticator, clock, _session = env
    challenge = service.enrollment_challenge("owner")
    with pytest.raises(StepUpError, match="ENROLLMENT_CHALLENGE_INVALID"):
        service.take_enrollment_challenge("owner", b"x" * 32)  # client-chosen challenge refused
    challenge = service.enrollment_challenge("owner")
    with pytest.raises(StepUpError, match="ENROLLMENT_CHALLENGE_INVALID"):
        service.take_enrollment_challenge("intruder", challenge)
    challenge = service.enrollment_challenge("owner")
    assert service.take_enrollment_challenge("owner", challenge) == challenge
    with pytest.raises(StepUpError, match="ENROLLMENT_CHALLENGE_INVALID"):
        service.take_enrollment_challenge("owner", challenge)  # single use
    challenge = service.enrollment_challenge("owner")
    clock.now += timedelta(seconds=181)
    with pytest.raises(StepUpError, match="ENROLLMENT_CHALLENGE_INVALID"):
        service.take_enrollment_challenge("owner", challenge)
    del authenticator
