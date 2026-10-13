"""Privileged operations: authenticated two-person grant, bound request, authenticated ledger (CR-07)."""
from __future__ import annotations

import json
import logging
import os
import stat
import sys
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from machine_support import NOW, FakeProbe, enroll, host_key, lan

from pados.compute_node.identity_providers import SecurityProfile
from pados.enterprise.secrets import EnvironmentSecretResolver
from pados.execution.context import canonical_hash
from pados.machines.errors import MachineError
from pados.machines.inventory import InventoryStore, MachineInventory
from pados.machines.keys import home_base_key
from pados.machines.operators import (
    OPERATOR_REGISTRY_SCHEMA,
    Challenge,
    LocalOperator,
    OperatorAuthenticator,
    OperatorProof,
    ProofRole,
    compute_proof,
)
from pados.machines.privilege import (
    REMOTE_REFUSED,
    GrantLedger,
    GrantRequest,
    MachineOperation,
    OperationClass,
    PrivilegedCommand,
    PrivilegedGrant,
    authorize_privileged,
    consume_authorization,
    issue_grant,
    operation_class,
    request_for,
)
from pados.machines.profile import validated
from pados.machines.ux import check_machine
from pados.remote.auth import RemoteActor, RemoteSession

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and mode bits")
REASON = "rotate the build cache on the Mac"
SESSION = RemoteSession(actor=RemoteActor(actor_id="alice", credential_hash="A" * 64,
                                          projects=frozenset({"proj-1"}), may_approve=True), session_id="s-1")
SHELL_COMMANDS = {"restart_agent": PrivilegedCommand("restart_agent", MachineOperation.SHELL,
                                                     frozenset({"service"}))}
# HMAC key material is assembled at runtime (never a credential-shaped literal in the tree).
KEYS = {"PADOS_HOME_BASE_KEY": "h" * 40, "PADOS_OPERATOR_ALICE": "a" * 20 + "1" * 20,
        "PADOS_OPERATOR_BOB": "b" * 20 + "2" * 20}
OPERATOR_KEY = {"alice": KEYS["PADOS_OPERATOR_ALICE"], "bob": KEYS["PADOS_OPERATOR_BOB"]}


@dataclass
class World:
    store: InventoryStore
    inventory: MachineInventory
    ledger: GrantLedger
    authenticator: OperatorAuthenticator

    @property
    def machine_id(self) -> str:
        return self.inventory.machines[0].machine_id

    def request(self, operation: str = "TERMINAL", **options: Any) -> GrantRequest:
        return request_for(machine_id=self.machine_id, operation=operation,
                           project_id=options.pop("project_id", "proj-1"), **options)

    def challenge(self, request: GrantRequest | None = None) -> Challenge:
        return self.authenticator.challenge(request or self.request(), now=NOW)

    @staticmethod
    def proofs(challenge: Challenge, requester: str = "alice",
               approver: str = "bob") -> tuple[OperatorProof, OperatorProof]:
        return (OperatorProof(requester, compute_proof(OPERATOR_KEY[requester], challenge, ProofRole.REQUESTER,
                                                       requester)),
                OperatorProof(approver, compute_proof(OPERATOR_KEY[approver], challenge, ProofRole.APPROVER,
                                                      approver)))

    def issue(self, request: GrantRequest, challenge: Challenge, requester: object, approver: object,
              **options: Any) -> PrivilegedGrant:
        return issue_grant(request, challenge=challenge, requester=requester, approver=approver,
                           authenticator=self.authenticator, ledger=self.ledger,
                           inventory=options.pop("inventory", self.inventory), reason=options.pop("reason", REASON),
                           now=options.pop("now", NOW), **options)

    def grant(self, request: GrantRequest | None = None, *, requester: str = "alice", approver: str = "bob",
              **options: Any) -> PrivilegedGrant:
        request = request or self.request()
        challenge = self.challenge(request)
        return self.issue(request, challenge, *self.proofs(challenge, requester, approver), **options)

    def authorize(self, grant: object, request: GrantRequest | None = None, **options: Any) -> Any:
        request = request or self.request()
        return authorize_privileged(
            grant, operation=request.operation, machine_id=request.machine_id, project_id=request.project_id,
            command_id=request.command_id, arguments=dict(request.arguments), ledger=self.ledger,
            inventory=options.pop("inventory", self.inventory), now=options.pop("now", NOW + timedelta(minutes=1)),
            **options)

    def execute(self, authorization: object, request: GrantRequest | None = None, **options: Any) -> Any:
        request = request or self.request()
        return consume_authorization(
            authorization, operation=request.operation, machine_id=request.machine_id,
            project_id=request.project_id, command_id=request.command_id, arguments=dict(request.arguments),
            ledger=self.ledger, now=options.pop("now", NOW + timedelta(minutes=2)), **options)


def write_registry(path: Path, operators: dict[str, str]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": OPERATOR_REGISTRY_SCHEMA, "operators": [
        {"operator_id": op, "proof_key_ref": ref} for op, ref in operators.items()]}))
    os.chmod(path, 0o600)


def resolver() -> EnvironmentSecretResolver:
    return EnvironmentSecretResolver(dict(KEYS))


@pytest.fixture
def world(tmp_path: Path, store: InventoryStore, probe: FakeProbe) -> World:
    enroll(store, probe)
    home = home_base_key("secretref://env/PADOS_HOME_BASE_KEY", resolver=resolver())
    registry = tmp_path / "authority" / "operators.json"
    write_registry(registry, {"alice": "secretref://env/PADOS_OPERATOR_ALICE",
                              "bob": "secretref://env/PADOS_OPERATOR_BOB"})
    ledger = GrantLedger(tmp_path / "authority" / "grants.jsonl", home_key=home)
    return World(store, store.load(), ledger, OperatorAuthenticator(registry, home_key=home, resolver=resolver()))


def test_operation_classes() -> None:
    for op in ("TERMINAL", "SHELL", "ADMIN", "FILE_TRANSFER"):
        assert operation_class(op) is OperationClass.PRIVILEGED_OPERATION
    assert operation_class("CONNECTION_TEST") is OperationClass.ORDINARY
    with pytest.raises(MachineError, match="MACHINE_OPERATION_UNKNOWN"):
        operation_class("RUN_ANYTHING")


@pytest.mark.parametrize("operation", ["TERMINAL", "FILE_TRANSFER"])
def test_privileged_operation_requires_a_grant(world: World, operation: str) -> None:
    with pytest.raises(MachineError, match="PRIVILEGED_GRANT_REQUIRED"):
        world.authorize(None, world.request(operation))


def test_full_flow_issue_authorize_execute_once(world: World) -> None:
    grant = world.grant()
    assert (grant.actor, grant.approver) == ("alice", "bob")
    authorization = world.authorize(grant)
    assert authorization.request_digest == world.request().digest
    world.execute(authorization)
    with pytest.raises(MachineError, match="PRIVILEGED_AUTHORIZATION_ALREADY_EXECUTED"):
        world.execute(authorization, now=NOW + timedelta(minutes=3))
    with pytest.raises(MachineError, match="PRIVILEGED_GRANT_ALREADY_USED"):
        world.authorize(grant)
    events = [e["record"]["event"] for e in world.ledger.entries()]
    assert events == ["ISSUED", "CONSUMED", "EXECUTED", "DENIED", "DENIED"]


# MC-01: no caller-constructed actor is proof.
def test_cr07_mc01_caller_constructed_operators_are_not_proof(world: World) -> None:
    request = world.request()
    challenge = world.challenge(request)
    forged = LocalOperator("alice", ProofRole.REQUESTER, challenge.challenge_id, "0" * 64)
    with pytest.raises(MachineError, match="OPERATOR_PROOF_REQUIRED"):
        world.issue(request, challenge, forged, forged)
    req, _ = world.proofs(challenge)
    with pytest.raises(MachineError, match="OPERATOR_PROOF_INVALID"):
        world.issue(request, challenge, req, OperatorProof("bob", "f" * 64))
    bob_as_requester = OperatorProof("bob", compute_proof(OPERATOR_KEY["bob"], challenge, ProofRole.REQUESTER,
                                                          "bob"))
    with pytest.raises(MachineError, match="OPERATOR_PROOF_INVALID"):  # role is bound into the proof
        world.issue(request, challenge, req, bob_as_requester)
    with pytest.raises(MachineError, match="OPERATOR_UNKNOWN"):
        world.issue(request, challenge, req, OperatorProof("mallory", "f" * 64))
    assert not [e for e in world.ledger.entries() if e["record"]["event"] == "ISSUED"]


def test_cr07_mc01_challenges_are_pados_issued_bound_fresh_and_single_use(world: World) -> None:
    request = world.request()
    challenge = world.challenge(request)
    invented = Challenge.model_validate({**challenge.model_dump(), "challenge_mac": "0" * 64})
    with pytest.raises(MachineError, match="CHALLENGE_INVALID"):
        world.issue(request, invented, *world.proofs(invented))
    proofs = world.proofs(challenge)
    with pytest.raises(MachineError, match="CHALLENGE_REQUEST_MISMATCH"):
        world.issue(world.request("FILE_TRANSFER"), challenge, *proofs)
    with pytest.raises(MachineError, match="CHALLENGE_EXPIRED"):
        world.issue(request, challenge, *proofs, now=NOW + timedelta(minutes=3))
    world.issue(request, challenge, *proofs)
    with pytest.raises(MachineError, match="CHALLENGE_ALREADY_USED"):
        world.issue(request, challenge, *proofs)


def test_cr07_mc01_approver_must_differ_unless_local_single_user(world: World) -> None:
    with pytest.raises(MachineError, match="PRIVILEGED_GRANT_SELF_APPROVAL_FORBIDDEN"):
        world.grant(requester="alice", approver="alice")
    with pytest.raises(MachineError, match="PRIVILEGED_GRANT_SELF_APPROVAL_FORBIDDEN"):
        world.grant(requester="alice", approver="alice", break_glass=True, ttl=timedelta(minutes=2))
    single = world.store.update(
        lambda inv: inv.model_copy(update={"security_profile": SecurityProfile.LOCAL_SINGLE_USER}))
    grant = world.grant(requester="alice", approver="alice", inventory=single)
    assert world.authorize(grant, inventory=single).actor == "alice"
    other = world.grant(requester="alice", approver="alice", inventory=single)
    with pytest.raises(MachineError, match="PRIVILEGED_GRANT_PROFILE_MISMATCH"):
        world.authorize(other)


def test_cr07_mc01_ledger_records_ids_and_proof_digests_only(world: World) -> None:
    request = world.request()
    challenge = world.challenge(request)
    req, app = world.proofs(challenge)
    world.issue(request, challenge, req, app)
    text = world.ledger.path.read_text() + world.ledger.head_path.read_text()
    assert req.proof not in text and app.proof not in text
    assert all(value not in text for value in KEYS.values())
    issued = world.ledger.entries()[0]["record"]
    assert (issued["actor"], issued["approver"]) == ("alice", "bob")
    assert len(issued["requester_proof_digest"]) == 64 and issued["challenge_id"] == challenge.challenge_id


@pytest.mark.parametrize("where", ["session", "grant", "requester", "approver", "execution"])
def test_remote_session_can_never_grant_or_satisfy_a_privileged_operation(world: World, where: str) -> None:
    request = world.request()
    with pytest.raises(MachineError, match=REMOTE_REFUSED):
        if where == "session":
            world.authorize(world.grant(), session=SESSION)
        elif where == "grant":
            world.authorize(SESSION)
        elif where in {"requester", "approver"}:
            challenge = world.challenge(request)
            req, app = world.proofs(challenge)
            world.issue(request, challenge, SESSION if where == "requester" else req,
                        SESSION.actor if where == "approver" else app)
        else:
            world.execute(world.authorize(world.grant()), session=SESSION)
    denied = [e["record"] for e in world.ledger.entries() if e["record"]["event"] == "DENIED"]
    assert denied and denied[-1]["decision_code"] == REMOTE_REFUSED


# MC-02: ledger permissions, authentication and rollback.
@POSIX
def test_cr07_mc02_ledger_files_must_be_owner_only_in_a_protected_parent(world: World) -> None:
    world.grant()
    os.chmod(world.ledger.path, 0o644)
    with pytest.raises(MachineError, match="GRANT_LEDGER_PERMISSIONS_TOO_OPEN"):
        world.ledger.entries()
    os.chmod(world.ledger.path, 0o600)
    os.chmod(world.ledger.head_path, 0o640)
    with pytest.raises(MachineError, match="GRANT_LEDGER_HEAD_PERMISSIONS_TOO_OPEN"):
        world.ledger.entries()
    os.chmod(world.ledger.head_path, 0o600)
    parent = world.ledger.path.parent
    os.chmod(parent, 0o777)
    try:
        with pytest.raises(MachineError, match="GRANT_LEDGER_PARENT_INSECURE"):
            world.ledger.entries()
    finally:
        os.chmod(parent, 0o700)
    assert stat.S_IMODE(world.ledger.path.stat().st_mode) == 0o600
    assert len(world.ledger.entries()) == 1


@POSIX
def test_cr07_mc02_symlinked_ledger_is_refused(world: World, tmp_path: Path) -> None:
    world.grant()
    target = tmp_path / "authority" / "elsewhere.jsonl"
    world.ledger.path.rename(target)
    world.ledger.path.symlink_to(target)
    with pytest.raises(MachineError, match="GRANT_LEDGER_OPEN_REFUSED"):
        world.ledger.entries()


def test_cr07_mc02_fabricated_issued_record_without_the_key_is_refused(world: World) -> None:
    grant = world.grant()
    forged = validated(PrivilegedGrant, {**grant.model_dump(), "grant_id": "pg-" + "1" * 20},
                       prefix="PRIVILEGED_GRANT")
    lines = world.ledger.path.read_text().splitlines()
    entry = json.loads(lines[0])
    entry["prev_hash"] = json.loads(lines[-1])["entry_hash"]
    entry["record"] = {**entry["record"], "grant_id": forged.grant_id, "grant_hash": forged.grant_hash}
    entry["entry_hash"] = canonical_hash({"prev_hash": entry["prev_hash"], "record": entry["record"]})
    world.ledger.path.write_text("\n".join([*lines, json.dumps(entry)]) + "\n")
    with pytest.raises(MachineError, match="GRANT_LEDGER_CHAIN_INVALID"):
        world.authorize(forged)


def test_cr07_mc02_truncating_consumed_records_is_detected_as_rollback(world: World) -> None:
    grant = world.grant()
    world.authorize(grant)
    lines = world.ledger.path.read_text().splitlines()
    world.ledger.path.write_text(lines[0] + "\n")  # drop CONSUMED to replay the grant
    with pytest.raises(MachineError, match="GRANT_LEDGER_ROLLBACK_DETECTED"):
        world.authorize(grant)
    world.ledger.path.unlink()
    with pytest.raises(MachineError, match="GRANT_LEDGER_ROLLBACK_DETECTED"):
        world.ledger.entries()


def test_cr07_mc02_head_is_authenticated_and_required(world: World) -> None:
    world.grant()
    head = json.loads(world.ledger.head_path.read_text())
    world.ledger.head_path.write_text(json.dumps({**head, "count": 0}))
    with pytest.raises(MachineError, match="GRANT_LEDGER_HEAD_INVALID"):
        world.ledger.entries()
    world.ledger.head_path.unlink()
    with pytest.raises(MachineError, match="GRANT_LEDGER_HEAD_MISSING"):
        world.ledger.entries()


def test_cr07_mc02_wrong_or_missing_home_base_key_fails_closed(world: World, tmp_path: Path) -> None:
    world.grant()
    ref = "secretref://env/PADOS_HOME_BASE_KEY"
    wrong = GrantLedger(world.ledger.path, home_key=home_base_key(
        ref, resolver=EnvironmentSecretResolver({"PADOS_HOME_BASE_KEY": "z" * 40})))
    with pytest.raises(MachineError, match="GRANT_LEDGER_CHAIN_INVALID"):
        wrong.entries()
    missing = GrantLedger(world.ledger.path, home_key=home_base_key(ref, resolver=EnvironmentSecretResolver({})))
    with pytest.raises(MachineError, match="HOME_BASE_KEY_UNAVAILABLE"):
        missing.entries()
    short = GrantLedger(tmp_path / "authority" / "x.jsonl", home_key=home_base_key(
        ref, resolver=EnvironmentSecretResolver({"PADOS_HOME_BASE_KEY": "short"})))
    with pytest.raises(MachineError, match="HOME_BASE_KEY_TOO_SHORT"):
        short.entries()


# MC-04: every bound field and the full request digest; execution consumes once.
@pytest.mark.parametrize(("operation", "change", "code"), [
    ("SHELL", {"project_id": "proj-2"}, "PRIVILEGED_GRANT_PROJECT_MISMATCH"),
    ("FILE_TRANSFER", {}, "PRIVILEGED_GRANT_OPERATION_MISMATCH"),
    ("SHELL", {"command_id": "other_command"}, "PRIVILEGED_GRANT_COMMAND_MISMATCH"),
    ("SHELL", {"arguments": {"service": "other-service"}}, "PRIVILEGED_REQUEST_DIGEST_MISMATCH"),
])
def test_cr07_mc04_authorization_compares_every_bound_field(world: World, operation: str,
                                                             change: dict[str, Any], code: str) -> None:
    bound = world.request("SHELL", command_id="restart_agent", arguments={"service": "pados-agent"})
    grant = world.grant(bound)
    fields: dict[str, Any] = {"command_id": "restart_agent", "arguments": {"service": "pados-agent"}} \
        if operation == "SHELL" else {}
    asked = world.request(operation, **{**fields, **change})
    with pytest.raises(MachineError, match=code):
        world.authorize(grant, asked, commands=SHELL_COMMANDS)
    assert world.authorize(grant, bound, commands=SHELL_COMMANDS).request_digest == bound.digest


def test_cr07_mc04_execution_boundary_requires_the_authorized_request(world: World) -> None:
    bound = world.request("SHELL", command_id="restart_agent", arguments={"service": "pados-agent"})
    authorization = world.authorize(world.grant(bound), bound, commands=SHELL_COMMANDS)
    with pytest.raises(MachineError, match="PRIVILEGED_EXECUTION_REQUEST_MISMATCH"):
        world.execute(authorization, world.request("SHELL", project_id="proj-2", command_id="restart_agent",
                                                   arguments={"service": "pados-agent"}))
    with pytest.raises(MachineError, match="PRIVILEGED_EXECUTION_REQUEST_MISMATCH"):
        world.execute(authorization, world.request("SHELL", command_id="restart_agent", arguments={"service": "x"}))
    with pytest.raises(MachineError, match="PRIVILEGED_AUTHORIZATION_EXPIRED"):
        world.execute(authorization, bound, now=NOW + timedelta(minutes=10))
    with pytest.raises(MachineError, match="PRIVILEGED_AUTHORIZATION_NOT_RECORDED"):
        world.execute(replace(authorization, authorization_id="pa-" + "0" * 20), bound)
    entry = world.execute(authorization, bound)
    assert entry["record"]["request_digest"] == bound.digest
    with pytest.raises(MachineError, match="PRIVILEGED_AUTHORIZATION_ALREADY_EXECUTED"):
        world.execute(authorization, bound)


def test_grant_expiry_and_lifetime_bounds(world: World) -> None:
    grant = world.grant(ttl=timedelta(minutes=15))
    with pytest.raises(MachineError, match="PRIVILEGED_GRANT_EXPIRED"):
        world.authorize(grant, now=NOW + timedelta(minutes=15))
    with pytest.raises(MachineError, match="PRIVILEGED_GRANT_NOT_YET_VALID"):
        world.authorize(grant, now=NOW - timedelta(seconds=1))
    with pytest.raises(MachineError, match="PRIVILEGED_GRANT_LIFETIME_INVALID"):
        world.grant(ttl=timedelta(minutes=16))
    with pytest.raises(MachineError, match="PRIVILEGED_GRANT_LIFETIME_INVALID"):
        world.grant(ttl=timedelta(minutes=6), break_glass=True)
    glass = world.grant(ttl=timedelta(minutes=5), break_glass=True)
    assert world.authorize(glass).break_glass is True


def test_forged_or_stretched_grants_are_refused(world: World) -> None:
    issued = world.grant(ttl=timedelta(minutes=2))
    forged = validated(PrivilegedGrant, {**issued.model_dump(), "grant_id": "pg-" + "1" * 20},
                       prefix="PRIVILEGED_GRANT")
    with pytest.raises(MachineError, match="PRIVILEGED_GRANT_NOT_RECORDED"):
        world.authorize(forged)
    stretched = validated(PrivilegedGrant, {**issued.model_dump(), "expires_at": NOW + timedelta(minutes=10)},
                          prefix="PRIVILEGED_GRANT")
    with pytest.raises(MachineError, match="PRIVILEGED_GRANT_NOT_RECORDED"):
        world.authorize(stretched, now=NOW + timedelta(minutes=5))


def test_shell_requires_a_registered_command_and_no_shell_is_exposed_by_default(world: World) -> None:
    with pytest.raises(MachineError, match="PRIVILEGED_GRANT_COMMAND_SCOPE_INVALID"):
        world.request("SHELL")
    bound = world.request("SHELL", command_id="restart_agent")
    grant = world.grant(bound)
    with pytest.raises(MachineError, match="PRIVILEGED_COMMAND_NOT_REGISTERED"):
        world.authorize(grant, bound)
    with pytest.raises(MachineError, match="PRIVILEGED_COMMAND_ARGUMENT_INVALID"):
        world.request("SHELL", command_id="restart_agent", arguments={"service": "a; rm -rf /"})
    with pytest.raises(MachineError, match="PRIVILEGED_COMMAND_ARGUMENT_INVALID"):
        world.request("SHELL", command_id="restart_agent", arguments={"service": "--all"})
    other = world.request("SHELL", command_id="restart_agent", arguments={"cmd": "x"})
    with pytest.raises(MachineError, match="PRIVILEGED_COMMAND_ARGUMENT_SCHEMA_INVALID"):
        world.authorize(world.grant(other), other, commands=SHELL_COMMANDS)
    assert world.authorize(grant, bound, commands=SHELL_COMMANDS).operation is MachineOperation.SHELL


def test_unauthorized_machine_refuses_privileged_operations(world: World, probe: FakeProbe) -> None:
    grant = world.grant()
    probe.place(lan(20), host_key("attacker"))
    check_machine(world.store, "Mac", probe, now=NOW)
    with pytest.raises(MachineError, match="PRIVILEGED_MACHINE_UNAUTHORIZED"):
        world.authorize(grant, inventory=world.store.load())


def test_ledger_holds_no_command_text_and_chain_tampering_fails_closed(
        world: World, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    bound = world.request("SHELL", command_id="restart_agent", arguments={"service": "unique-arg-7f3a"})
    world.authorize(world.grant(bound), bound, commands=SHELL_COMMANDS)
    text = world.ledger.path.read_text()
    assert "unique-arg-7f3a" not in text and "unique-arg-7f3a" not in caplog.text
    for line in text.splitlines():
        record = json.loads(line)["record"]
        assert not {"command", "arguments", "output", "scrollback", "stdout"} & set(record)
    lines = text.splitlines()
    tampered = json.loads(lines[0])
    tampered["record"]["operation"] = "ADMIN"
    world.ledger.path.write_text(json.dumps(tampered) + "\n" + "\n".join(lines[1:]) + "\n")
    with pytest.raises(MachineError, match="GRANT_LEDGER_CHAIN_INVALID"):
        world.ledger.entries()


def test_grant_reason_is_minimized(world: World) -> None:
    request = world.request()
    challenge = world.challenge(request)
    with pytest.raises(MachineError, match="PRIVILEGED_GRANT_CREDENTIAL_SHAPED_VALUE"):
        world.issue(request, challenge, *world.proofs(challenge), reason="tok" + "en=abcdefgh12345678")


@POSIX
def test_operator_registry_must_be_owner_only(world: World) -> None:
    os.chmod(world.authenticator.registry_path, 0o644)
    request = world.request()
    challenge = world.challenge(request)
    req, _ = world.proofs(challenge)
    with pytest.raises(MachineError, match="OPERATOR_REGISTRY_PERMISSIONS_TOO_OPEN"):
        world.authenticator.verify(req, role=ProofRole.REQUESTER, challenge=challenge, request=request, now=NOW)
