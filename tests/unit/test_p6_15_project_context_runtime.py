from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

from pados.corpus import runtime

PROJECT_ID = "a45eed63-f747-49f7-9f48-05aaa6eb3e72"


class _Result:
    def fetchone(self) -> tuple[int]:
        return (0,)


class _Connection:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, *_args: object) -> _Result:
        return _Result()


def test_ingest_projects_context_before_any_document_pipeline_or_persistence(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root = tmp_path / "approved"
    root.mkdir()
    pdf = root / "document.pdf"
    pdf.write_bytes(b"fixture")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    authorization = tmp_path / "authorization.json"
    authorization.write_text("{}", encoding="utf-8")
    events: list[str] = []

    monkeypatch.setattr(runtime, "PROJECT_ID", PROJECT_ID)
    monkeypatch.setattr(runtime, "APPROVED_ROOT", root)
    monkeypatch.setattr(runtime, "ISOLATED_DATABASE", "pados_corpus_runtime")
    monkeypatch.setattr(runtime, "OPERATIONAL_DATABASE", "pados_local_runtime")
    monkeypatch.setattr(runtime, "POLICY", "KEYWORD_ONLY")
    monkeypatch.setattr(runtime, "_doctor_or_raise", lambda: {"provenance": {}})
    monkeypatch.setattr(
        runtime,
        "_verify_authorization",
        lambda *_args, **_kwargs: ({"batch_id": "batch-id"}, {}, [pdf]),
    )
    monkeypatch.setattr(
        runtime,
        "ensure_project_context_projection",
        lambda **_kwargs: events.append("projection") or {"status": "CREATED"},
    )
    monkeypatch.setattr(runtime, "_connect", lambda _database: _Connection())
    monkeypatch.setattr(runtime, "_settings", lambda _database: object())

    def pipeline(*_args: object, **_kwargs: object) -> dict[str, Any]:
        events.append("pipeline")
        return {"receipt": {"idempotency": "NEW_BATCH_PUBLISHED"}, "documents": []}

    monkeypatch.setattr(runtime, "run_fixture_pilot", pipeline)

    def persist(*_args: object, **_kwargs: object) -> str:
        events.append("persist")
        return "batch-id"

    monkeypatch.setattr(runtime, "persist_pilot_result", persist)

    @contextmanager
    def fake_session_scope(_settings: object):
        yield object()

    monkeypatch.setattr(runtime, "session_scope", fake_session_scope)
    result = runtime.ingest(manifest_path=manifest, authorization_path=authorization)

    assert events == ["projection", "pipeline", "persist"]
    assert result["project_context_projection"]["status"] == "CREATED"
