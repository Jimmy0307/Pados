"""RC2 SEC-2: no project context reaches a cloud worker without a data-classification gate.

The Codex CLI Maker sends the governed contract (with its bounded project context) to a cloud provider. Before the
provider starts, the project's recorded classification and any bundled enterprise evidence must be known and within
the configured cloud ceiling (``PADOS_PRODUCT_LOOP_CLOUD_MAX_CLASSIFICATION``, default INTERNAL). Unknown, missing,
or over-ceiling data fails closed; RESTRICTED never leaves the host, whatever is configured.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pados.product_loop.codex_provider import CodexCliMaker
from tests.unit.test_pados_codex_provider import codex_worker, contract


def _maker(root: Path, calls: list[Any], *, source: Any, ceiling: str | None = None) -> CodexCliMaker:
    def runner(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append(argv)
        Path(kwargs["cwd"], "result.py").write_text("result = 42\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    settings = SimpleNamespace(product_loop_cloud_max_classification=ceiling) if ceiling else object()
    return CodexCliMaker(root, settings=settings, runner=runner,
                         worker_discovery=lambda _settings: {"codex-cli": codex_worker()},
                         executable_resolver=lambda: "/usr/bin/codex", classification_source=source)


def _evidence(level: str) -> dict[str, Any]:
    return {"extensions": {"enterprise_evidence": {"package": {"evidence": [{"classification": level}]}}}}


def _run(maker: CodexCliMaker, root: Path, **updates: Any) -> Any:
    return asyncio.run(maker.make(contract(root, **updates), run_id="run-1", attempt=1))


@pytest.mark.parametrize("recorded", ["confidential", "RESTRICTED", "secret", "", None])
def test_over_ceiling_or_unknown_project_classification_never_reaches_the_cloud(tmp_path: Path, recorded: Any) -> None:
    calls: list[Any] = []
    result = _run(_maker(tmp_path, calls, source=lambda _project: recorded), tmp_path)
    assert result.status.value == "BLOCKED"
    assert result.failure_classification.value == "AUTHORITY_BLOCK"
    assert "CLOUD_EGRESS" in result.summary
    assert calls == []


def test_without_a_classification_source_nothing_is_sent(tmp_path: Path) -> None:
    calls: list[Any] = []
    result = _run(_maker(tmp_path, calls, source=None), tmp_path)
    assert result.status.value == "BLOCKED" and calls == []


def test_restricted_evidence_in_a_public_project_is_refused(tmp_path: Path) -> None:
    calls: list[Any] = []
    result = _run(_maker(tmp_path, calls, source=lambda _project: "public"), tmp_path,
                  context_snapshot=_evidence("RESTRICTED"))
    assert result.status.value == "BLOCKED" and calls == []


def test_a_tighter_ceiling_refuses_internal_data(tmp_path: Path) -> None:
    calls: list[Any] = []
    result = _run(_maker(tmp_path, calls, source=lambda _project: "internal", ceiling="PUBLIC"), tmp_path)
    assert result.status.value == "BLOCKED" and calls == []


@pytest.mark.parametrize("ceiling", ["RESTRICTED", "everything"])
def test_a_ceiling_that_would_release_restricted_or_is_unknown_refuses_everything(tmp_path: Path, ceiling: str) -> None:
    calls: list[Any] = []
    result = _run(_maker(tmp_path, calls, source=lambda _project: "public", ceiling=ceiling), tmp_path)
    assert result.status.value == "BLOCKED" and calls == []


def test_the_classification_is_read_for_the_contract_project(tmp_path: Path) -> None:
    calls: list[Any] = []
    asked: list[str] = []
    result = _run(_maker(tmp_path, calls, source=lambda project: asked.append(project) or "internal"), tmp_path)
    assert asked == ["project-1"]
    assert result.status.value == "COMPLETED" and len(calls) == 1


def test_data_within_the_default_ceiling_still_runs(tmp_path: Path) -> None:
    calls: list[Any] = []
    result = _run(_maker(tmp_path, calls, source=lambda _project: "public"), tmp_path,
                  context_snapshot=_evidence("INTERNAL"))
    assert result.status.value == "COMPLETED" and len(calls) == 1


def test_the_production_service_wires_the_project_classification_into_the_cloud_maker(tmp_path: Path) -> None:
    from pados.product_loop.routes import build_product_loop_service
    from tests.unit.test_p6_21j_adoption_gap_repair import _settings

    service = build_product_loop_service(_settings(tmp_path / "workspace", tmp_path / "objects"))
    assert isinstance(service.maker, CodexCliMaker)
    assert service.maker.classification_source == service.state_gateway.project_classification
