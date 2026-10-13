"""Spawn-safe worker for the CR13b-02 consume race (imported by child processes; no pytest imports)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from pados.capability.decision_ledger import FileConsumedDecisionLedger
from pados.capability.human_decision import HmacHumanDecisionVerifier, HumanDecisionRefused
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary


def race_consume(path: str, key_name: str, key: str, approver: str, record: dict[str, str], task_id: str,
                 plan_hash: str, now_iso: str, barrier: Any, results: Any) -> None:
    """One process: its OWN verifier and ledger instance over the shared file, released by the barrier."""
    verifier = HmacHumanDecisionVerifier(
        operator_keys={approver: f"secretref://env/{key_name}"},
        boundary=SecretBoundary(EnvironmentSecretResolver({key_name: key})),
        ledger=FileConsumedDecisionLedger(Path(path)), environ={})
    barrier.wait()
    try:
        verified = verifier.verify_and_consume(record, task_id=task_id, plan_hash=plan_hash,
                                               now=datetime.fromisoformat(now_iso))
    except HumanDecisionRefused as exc:
        results.put(exc.code)
    except Exception as exc:  # noqa: BLE001 - reported to the parent as a failure code
        results.put(f"UNEXPECTED:{type(exc).__name__}")
    else:
        results.put(verified.decision_ref)
