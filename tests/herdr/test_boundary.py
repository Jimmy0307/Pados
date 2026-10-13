"""Authority boundary: Herdr is telemetry only and has no path to PADOS authority (SH-H01)."""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pados.herdr.boundary import (
    AUTHORITY_TERMS,
    MEANING_SCOPE,
    STATE_MEANING,
    HerdrAuthorityViolation,
    TelemetryMeaning,
    assert_no_authority,
    telemetry_meaning,
)
from pados.herdr.contract import HerdrAgentKind, HerdrAgentState, HerdrTelemetryRecord
from pados.herdr.store import HerdrTelemetryStore
from tests.herdr.herdr_support import MACHINE, NOW, OTHER_MACHINE, Clock, adapter_for, pane_ref, session_ref

SRC = Path(__file__).resolve().parents[2] / "src"
HERDR_DIR = SRC / "pados" / "herdr"
HERDR_MODULES = sorted(HERDR_DIR.glob("*.py"))
# Authority, Memory, scheduling, routing, receipt, audit and remote-control surfaces.
FORBIDDEN_PREFIXES = (
    "pados.state_proof", "pados.memory", "pados.product_loop", "pados.worker", "pados.remote",
    "pados.control", "pados.loop", "pados.machines", "pados.execution.receipts", "pados.execution.authorization",
    "pados.db", "pados.api", "pados.operations", "pados.cli", "pados.gitops", "pados.compute_node",
    "pados.agents", "pados.providers", "pados.project_goal", "pados.knowledge", "pados.wiki",
)
ALLOWED_HERDR_IMPORTERS = {"pados/cli/main.py", "pados/operations/service.py"}


def _module_file(name: str) -> Path | None:
    base = SRC.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _direct_imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def _pados_closure(roots: list[str]) -> set[str]:
    seen: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in seen or not name.startswith("pados"):
            continue
        path = _module_file(name)
        if path is None:
            continue
        seen.add(name)
        parts = name.split(".")
        queue.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        queue.extend(_direct_imports(path))
    return seen


def _herdr_module_names() -> list[str]:
    return [f"pados.herdr.{p.stem}" if p.stem != "__init__" else "pados.herdr" for p in HERDR_MODULES]


# --- state semantics ------------------------------------------------------------------------

def test_every_state_maps_to_a_telemetry_only_observation() -> None:
    assert set(STATE_MEANING) == set(HerdrAgentState) and MEANING_SCOPE == "TELEMETRY_ONLY"
    for state, meaning in STATE_MEANING.items():
        assert telemetry_meaning(state) is meaning
        assert meaning.value.startswith("AGENT_") and meaning.value.endswith("_OBSERVED")
        assert not set(meaning.value.split("_")) & AUTHORITY_TERMS
    assert STATE_MEANING[HerdrAgentState.WORKING] is TelemetryMeaning.AGENT_ACTIVITY_OBSERVED


def _pados_enum_values() -> set[str]:
    values: set[str] = set()
    for path in (SRC / "pados").rglob("*.py"):
        if HERDR_DIR in path.parents:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, ast.ClassDef) and any(
                    isinstance(b, ast.Name) and b.id in {"StrEnum", "Enum"} for b in node.bases):
                for item in node.body:
                    if isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant) \
                            and isinstance(item.value.value, str):
                        values.add(item.value.value)
    return values


def test_telemetry_meanings_are_not_pados_run_job_or_acceptance_states() -> None:
    pados_values = _pados_enum_values()
    assert {"ACCEPTED", "COMPLETED"} & pados_values  # the scan sees PADOS state vocabularies
    assert not {m.value for m in TelemetryMeaning} & pados_values


def test_no_function_derives_acceptance_success_or_failure_from_herdr_state() -> None:
    authority_words = re.compile(r"accept|success|succeed|fail|approv|commit|verdict|promot|receipt|reject|"
                                 r"outcome|run_state|job_state|to_pados|as_pados")
    for path in HERDR_MODULES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert not authority_words.search(node.name.lower()), f"{path.name}:{node.name}"


# --- assert_no_authority ----------------------------------------------------------------------

def _row(**overrides: object) -> dict[str, object]:
    row = HerdrTelemetryRecord(machine_ref=MACHINE, session_ref=session_ref("s"), pane_ref=pane_ref("s", "p"),
                               collected_at=NOW).to_row()
    row.update(overrides)
    return row


def test_assert_no_authority_accepts_pure_telemetry() -> None:
    assert_no_authority(_row())


@pytest.mark.parametrize(("overrides", "code"), [
    ({"pados_status": "ACCEPTED"}, "HERDR_RECORD_SCHEMA_MISMATCH"),
    ({"result_receipt": {"ok": True}}, "HERDR_RECORD_SCHEMA_MISMATCH"),
    ({"semantic_authority": "FULL"}, "HERDR_CLAIMS_SEMANTIC_AUTHORITY"),
    ({"state": "ACCEPTED"}, "HERDR_STATE_NOT_TELEMETRY"),
    ({"state": "SUCCEEDED"}, "HERDR_STATE_NOT_TELEMETRY"),
    ({"health": "PASSED"}, "HERDR_HEALTH_INVALID"),
    ({"schema_id": "pados.result.v1"}, "HERDR_SCHEMA_ID_INVALID"),
    ({"agent_kind": "ACCEPTED"}, "HERDR_AGENT_KIND_INVALID"),
    ({"agent_kind": "Claude Code"}, "HERDR_AGENT_KIND_INVALID"),
    ({"agent": {"kind": "claude", "label": "ACCEPTED"}}, "HERDR_RECORD_SCHEMA_MISMATCH"),
    ({"source_observed_at": {"nested": "x"}}, "HERDR_VALUE_NOT_SCALAR"),
    ({"project_ref": "APPROVED"}, "HERDR_VALUE_IS_AUTHORITY_TERM"),
])
def test_assert_no_authority_refuses_authority_claims(overrides: dict[str, object], code: str) -> None:
    with pytest.raises(HerdrAuthorityViolation) as caught:
        assert_no_authority(_row(**overrides))
    assert caught.value.code == code
    row = _row()
    del row["semantic_authority"]
    with pytest.raises(HerdrAuthorityViolation):
        assert_no_authority(row)


# --- Herdr compromise cannot become semantic authority ------------------------------------------

def test_malicious_document_claiming_acceptance_has_zero_authority_effect(tmp_path: Path) -> None:
    clock = Clock()
    result = adapter_for("authority_claim.json", clock).collect()
    (record,) = result.records
    assert record.state is HerdrAgentState.UNKNOWN  # "ACCEPTED" is not a Herdr state
    assert record.agent_kind is HerdrAgentKind.OTHER  # "accepted" is not a known agent kind
    assert record.project_ref is None and record.work_package_ref is None
    assert record.machine_ref == MACHINE != OTHER_MACHINE and record.semantic_authority == "NONE"
    assert result.authority_claims_ignored >= 10 and result.identity_claims_ignored == 3
    store = HerdrTelemetryStore(tmp_path / "telemetry.json", clock=clock)
    store.record_collection(result)
    persisted = store.path.read_text(encoding="utf-8")
    for planted in ("ACCEPTED", "SUCCEEDED", "FULL", "attacker-box", OTHER_MACHINE, "run-1", "approver",
                    "PADOS memory", "forged", "s-alpha", "\"p-1\""):
        assert planted not in persisted, planted
    (row,) = store.latest()
    assert row["telemetry_meaning"] == "AGENT_STATE_NOT_OBSERVED" and row["semantic_authority"] == "NONE"
    assert set(result.summary()) == {"schema_id", "collected_at", "outcome", "source_health", "records",
                                     "error_code", "source_observed_at", "source_time_code",
                                     "terminal_content_dropped", "authority_claims_ignored",
                                     "identity_claims_ignored", "fields_dropped", "entries_refused",
                                     "refusal_codes", "semantic_authority"}


def test_importing_herdr_loads_no_authority_module() -> None:
    script = (
        "import json, sys\n"
        f"import {', '.join(_herdr_module_names())}\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('pados'))))\n"
    )
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60,
                               cwd=SRC.parent, check=True)
    loaded = json.loads(completed.stdout)
    assert "pados.herdr.adapter" in loaded
    assert [m for m in loaded if m.startswith(FORBIDDEN_PREFIXES)] == []


# --- static import boundary -----------------------------------------------------------------

def test_herdr_import_closure_excludes_authority_modules() -> None:
    closure = _pados_closure(_herdr_module_names())
    assert "pados.lifecycle.minimization" in closure
    assert sorted(m for m in closure if m.startswith(FORBIDDEN_PREFIXES)) == []
    for name in ("pados.memory.service", "pados.state_proof", "pados.worker.router", "pados.remote.approval",
                 "pados.remote.service", "pados.state_proof.audit_chain", "pados.product_loop.state",
                 "pados.product_loop.service"):
        assert name not in closure


def test_nothing_outside_herdr_imports_herdr_except_the_integrator_wiring() -> None:
    offenders = []
    for path in (SRC / "pados").rglob("*.py"):
        if HERDR_DIR in path.parents:
            continue
        relative = path.relative_to(SRC).as_posix()
        if any(name == "pados.herdr" or name.startswith("pados.herdr.") for name in _direct_imports(path)):
            if relative not in ALLOWED_HERDR_IMPORTERS:
                offenders.append(relative)
    assert offenders == []


def test_herdr_starts_no_threads_timers_or_schedulers_and_never_uses_a_shell() -> None:
    concurrency = ("threading", "sched", "asyncio", "concurrent", "multiprocessing", "signal", "socket",
                   "http", "urllib", "httpx", "requests")
    for path in HERDR_MODULES:
        text = path.read_text(encoding="utf-8")
        imports = _direct_imports(path)
        # adapter.py imports ``signal`` only for SIGKILL of the child's process group (CR-06 M04).
        allowed = {"signal", "signal.SIGKILL"} if path.name == "adapter.py" else set()
        assert not [m for m in imports - allowed if m.split(".")[0] in concurrency], path.name
        assert "shell=True" not in text and "os.system" not in text and "Timer(" not in text
        for handler in ("signal.signal(", "signal.alarm(", "setitimer(", "os.fork("):
            assert handler not in text, (path.name, handler)
        if path.name != "adapter.py":
            assert "subprocess" not in imports, path.name
