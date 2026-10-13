"""Codex CR-07b verification findings (MC-09..MC-11 and the partial closures of MC-01/02/04/05)."""
from __future__ import annotations

import os
import re
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from machine_support import NOW, FakeProbe, enroll
from pydantic import BaseModel, model_validator
from test_connection_states import RacingProbe
from test_privilege import World, resolver, world  # noqa: F401 - fixture re-export
from test_ssh_probe import Spawn, known_hosts, nonce_of, probe, profile

from pados.machines.connection import (
    OBSERVATION_FIELDS,
    PROBE_RELEVANT_FIELDS,
    AuthOutcome,
    classify,
    profile_revision,
)
from pados.machines.errors import MachineError
from pados.machines.inventory import InventoryStore
from pados.machines.keys import home_base_key
from pados.machines.operators import LocalOperator, ProofRole
from pados.machines.policy import CredentialPolicy
from pados.machines.privilege import GrantEvent, GrantLedger, _record
from pados.machines.profile import PROFILE_KEYS, ConnectionState, MachineProfile, validated
from pados.machines.ssh import AUTH_SIGNAL_PREFIX
from pados.machines.ux import check_machine

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and mode bits")


# MC-09 (+ MC-04): execution trusts only the authenticated ledger.
def test_cr07b_mc09_copied_authorization_with_extended_expiry_is_refused(world: World) -> None:  # noqa: F811
    authorization = world.authorize(world.grant(ttl=timedelta(minutes=5)))
    extended = replace(authorization, expires_at=authorization.expires_at + timedelta(hours=1))
    with pytest.raises(MachineError, match="PRIVILEGED_AUTHORIZATION_TAMPERED"):
        world.execute(extended, now=NOW + timedelta(minutes=30))
    with pytest.raises(MachineError, match="PRIVILEGED_AUTHORIZATION_TAMPERED"):
        world.execute(extended, now=NOW + timedelta(minutes=2))
    # The untouched authorization is checked against the ledger's expiry, not its own field.
    with pytest.raises(MachineError, match="PRIVILEGED_AUTHORIZATION_EXPIRED"):
        world.execute(authorization, now=NOW + timedelta(minutes=5))
    assert not [e for e in world.ledger.entries() if e["record"]["event"] == "EXECUTED"]


@pytest.mark.parametrize("change", [{"project_id": "proj-2"}, {"command_id": "x"}, {"actor": "bob"},
                                    {"break_glass": True}, {"grant_id": "pg-" + "1" * 20},
                                    {"request_digest": "0" * 64}])
def test_cr07b_mc09_any_supplied_field_differing_from_the_ledger_is_tampering(
        world: World, change: dict[str, Any]) -> None:  # noqa: F811
    authorization = world.authorize(world.grant())
    with pytest.raises(MachineError, match="PRIVILEGED_AUTHORIZATION_TAMPERED|NOT_RECORDED"):
        world.execute(replace(authorization, **change))
    assert world.execute(authorization)["record"]["event"] == "EXECUTED"


def test_cr07b_mc09_executed_record_is_derived_from_the_ledger(world: World) -> None:  # noqa: F811
    authorization = world.authorize(world.grant())
    executed = world.execute(authorization)["record"]
    consumed = next(e["record"] for e in world.ledger.entries() if e["record"]["event"] == "CONSUMED")
    for field in ("grant_id", "grant_hash", "request_digest", "machine_id", "operation", "project_id",
                  "expires_at", "actor", "approver"):
        assert executed[field] == consumed[field], field


# MC-01: no caller-built ISSUED record.
def test_cr07b_mc01_public_append_cannot_write_issued_or_transitions(world: World) -> None:  # noqa: F811
    grant = world.grant()
    issued = _record(GrantEvent.ISSUED, recorded_at=NOW, grant=grant)
    with pytest.raises(MachineError, match="GRANT_LEDGER_ISSUE_REQUIRES_PROOFS"):
        world.ledger.append(issued)
    with pytest.raises(MachineError, match="GRANT_LEDGER_TRANSITION_REQUIRES_AUTHORIZATION"):
        world.ledger.append({**issued, "event": "CONSUMED"})
    assert not hasattr(GrantLedger, "issue")


def test_cr07b_mc01_issued_writer_requires_the_verified_proofs(world: World) -> None:  # noqa: F811
    grant = world.grant()
    forged = (LocalOperator(grant.actor, ProofRole.REQUESTER, grant.challenge_id, "0" * 64),
              LocalOperator(grant.approver, ProofRole.APPROVER, grant.challenge_id, "0" * 64))
    other = grant.model_copy(update={"grant_id": "pg-" + "2" * 20})
    with pytest.raises(MachineError, match="GRANT_LEDGER_ISSUE_REQUIRES_PROOFS"):
        world.ledger._record_issued(other, NOW, forged)
    swapped = (LocalOperator(grant.actor, ProofRole.APPROVER, grant.challenge_id, grant.requester_proof_digest),
               LocalOperator(grant.approver, ProofRole.REQUESTER, grant.challenge_id, grant.approver_proof_digest))
    with pytest.raises(MachineError, match="GRANT_LEDGER_ISSUE_REQUIRES_PROOFS"):
        world.ledger._record_issued(other, NOW, swapped)


# MC-10: authentication success from an unforgeable local signal.
def test_cr07b_mc10_banner_claiming_authentication_with_permission_denied_is_not_online(tmp_path: Path) -> None:
    banner = b'Authenticated to mac ([peer]:22) using "publickey".\noperator@mac: Permission denied (publickey).\n'
    made, _, _ = probe(tmp_path, spawn=Spawn(banner, stdout=b"", hang=False))
    state, code = classify(profile(), made.check(profile(), known_hosts(tmp_path)))
    assert state is ConnectionState.AUTH_REQUIRED and code == "MACHINE_AUTH_REQUIRED"


def test_cr07b_mc10_banner_alone_never_authenticates(tmp_path: Path) -> None:
    made, _, spawn = probe(tmp_path, spawn=Spawn(b"Authenticated to mac\n", stdout=b"", hang=True))
    observation = made.check(profile(), known_hosts(tmp_path))
    assert observation.auth is AuthOutcome.AUTH_REQUIRED
    assert observation.error_code == "SSH_DEADLINE_WITHOUT_AUTHENTICATION" and spawn.processes[0].killed


def test_cr07b_mc10_correct_fresh_nonce_authenticates(tmp_path: Path) -> None:
    made, _, spawn = probe(tmp_path)
    assert made.check(profile(), known_hosts(tmp_path)).auth is AuthOutcome.AUTHENTICATED
    made.check(profile(), known_hosts(tmp_path))
    first, second = (nonce_of(argv) for argv, _ in spawn.calls)
    assert first != second and re.fullmatch(r"[0-9a-f]{32}", first)
    argv = spawn.calls[0][0]
    assert f"LocalCommand=echo {AUTH_SIGNAL_PREFIX}{first}" in argv and "PermitLocalCommand=yes" in argv
    assert not [item for item in argv if "verbose" in item.lower()]


@pytest.mark.parametrize("stdout", [
    lambda nonce: f"pados-auth-{'0' * 32}\n".encode(),  # stale / other run's nonce
    lambda nonce: f"pados-auth-{nonce}\nextra\n".encode(),  # anything besides exactly the nonce line
    lambda nonce: f"xpados-auth-{nonce}\n".encode(),
])
def test_cr07b_mc10_wrong_or_extra_stdout_is_refused(tmp_path: Path, stdout: Any) -> None:
    class Tampered(Spawn):
        def __call__(self, argv: list[str], env: dict[str, str]) -> Any:
            self.stdout = stdout(nonce_of(argv))
            return super().__call__(argv, env)

    made, _, _ = probe(tmp_path, spawn=Tampered(hang=False))
    observation = made.check(profile(), known_hosts(tmp_path))
    assert observation.auth is AuthOutcome.AUTH_REQUIRED
    assert observation.error_code == "SSH_AUTHENTICATION_SIGNAL_INVALID"


def test_cr07b_mc10_nonce_is_validated_before_it_reaches_localcommand(tmp_path: Path) -> None:
    made, _, _ = probe(tmp_path)
    with pytest.raises(MachineError, match="SSH_AUTH_NONCE_INVALID"):
        made.ssh_argv(profile(), "x", known_hosts(tmp_path), [], "abc; rm -rf /")


# MC-11: the revision covers every probe/classification input.
def test_cr07b_mc11_revision_fields_partition_the_profile_schema() -> None:
    assert set(PROBE_RELEVANT_FIELDS) | set(OBSERVATION_FIELDS) == set(PROFILE_KEYS)
    assert not set(PROBE_RELEVANT_FIELDS) & set(OBSERVATION_FIELDS)
    assert "herdr" in PROBE_RELEVANT_FIELDS


class _Recorder:
    """Proxy that records which profile attributes a probe or the classifier reads."""

    _ALIASES = {"host_key": ("host_public_key",), "effective_auth_method": ("auth_method", "key_auth_method")}

    def __init__(self, real: MachineProfile) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "seen", set())

    def __getattr__(self, name: str) -> Any:
        self.seen.update(self._ALIASES.get(name, (name,)))
        return getattr(self._real, name)


def test_cr07b_mc11_probe_and_classifier_read_only_revision_fields(tmp_path: Path) -> None:
    base = profile(auth_method="SSH_AGENT", auth_ref="ssh-agent://default")
    recorder = _Recorder(base)
    made, _, _ = probe(tmp_path)
    os.environ["SSH_AUTH_SOCK"] = "agent.sock"
    try:
        observation = made.check(recorder, known_hosts(tmp_path))  # type: ignore[arg-type]
    finally:
        del os.environ["SSH_AUTH_SOCK"]
    classify(recorder, observation)  # type: ignore[arg-type]
    assert recorder.seen and recorder.seen <= set(PROBE_RELEVANT_FIELDS), recorder.seen - set(PROBE_RELEVANT_FIELDS)
    assert isinstance(made.policy, CredentialPolicy)


def test_cr07b_mc11_herdr_change_during_a_probe_discards_the_observation(store: InventoryStore) -> None:
    racing = RacingProbe(store, {"roles": ["HERDR_MACHINE"], "herdr": {"enabled": True, "ref": "herdr://default"}})
    profile_ = enroll(store, racing)
    racing.armed = True
    with pytest.raises(MachineError, match="OBSERVATION_STALE_REVISION"):
        check_machine(store, "Mac", racing, now=NOW + timedelta(minutes=1))
    stored = store.load().get(profile_.machine_id)
    assert stored.herdr.enabled and stored.state_observed_at == NOW
    assert profile_revision(stored) != profile_revision(profile_)


# MC-05: validated() drops its input on every exit path.
class _Exploding(BaseModel):
    marker: str

    @model_validator(mode="before")
    @classmethod
    def explode(cls, data: Any) -> Any:
        raise RuntimeError("validator failure that pydantic does not wrap")


def test_cr07b_mc05_validated_clears_input_even_when_a_validator_raises_unwrapped() -> None:
    secret_marker = "do-not-keep-" + "7f3a" * 4
    with pytest.raises(RuntimeError) as caught:
        validated(_Exploding, {"marker": secret_marker}, prefix="TEST")
    tb = caught.value.__traceback__
    frames = []
    while tb is not None:
        frames.append(tb.tb_frame)
        tb = tb.tb_next
    mine = [f for f in frames if f.f_code.co_name == "validated"]
    assert mine and all("data" not in f.f_locals for f in mine)
    assert all(secret_marker not in repr(v) for f in mine for v in f.f_locals.values())


def test_cr07b_mc05_machine_error_from_a_validator_keeps_no_input() -> None:
    secret_marker = "do-not-keep-" + "9c1e" * 4
    with pytest.raises(MachineError) as caught:
        validated(MachineProfile, {"display_name": secret_marker + "‮"}, prefix="MACHINE_PROFILE")
    tb = caught.value.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_name == "validated":
            assert "data" not in tb.tb_frame.f_locals
        tb = tb.tb_next


# MC-02: ancestors up to the trusted root.
@POSIX
def test_cr07b_mc02_symlinked_or_writable_ancestor_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "pados"
    real = root / "real"
    real.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    (root / "link").symlink_to(real, target_is_directory=True)
    key = home_base_key("secretref://env/PADOS_HOME_BASE_KEY", resolver=resolver())
    with pytest.raises(MachineError, match="GRANT_LEDGER_PARENT_INSECURE"):
        GrantLedger(root / "link" / "sub" / "grants.jsonl", home_key=key, root=root).entries()
    nested = real / "nested"
    nested.mkdir(mode=0o700)
    ledger = GrantLedger(nested / "grants.jsonl", home_key=key, root=root)
    assert ledger.entries() == []
    os.chmod(real, 0o777)
    try:
        with pytest.raises(MachineError, match="GRANT_LEDGER_PARENT_INSECURE"):
            ledger.entries()
    finally:
        os.chmod(real, 0o700)
    with pytest.raises(MachineError, match="GRANT_LEDGER_OUTSIDE_ROOT"):
        GrantLedger(tmp_path / "elsewhere" / "grants.jsonl", home_key=key, root=root).entries()
    with pytest.raises(MachineError, match="CREDENTIAL_POLICY_OUTSIDE_ROOT"):
        CredentialPolicy.load(tmp_path / "policy.json", root=root)


def test_cr07b_mc02_ledger_rollback_of_a_matching_pair_is_a_documented_accepted_risk() -> None:
    text = (Path(__file__).resolve().parents[2] / "docs" / "machines" / "MACHINE_CONNECTION_V1.md").read_text()
    assert "older ledger" in text and "monotonic anchor" in text and "ACCEPTED_RISK" in text


def test_cr07b_fixture_probe_is_hermetic(probe: FakeProbe) -> None:  # noqa: F811
    assert probe.scans == []
