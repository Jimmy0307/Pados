from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from pados.config import Settings
from pados.corpus import operator
from pados.corpus.operator_config import (
    OperatorConfiguration,
    OperatorConfigurationError,
    resolve_operator_configuration,
)

PROJECT_ID = "325f8168-efec-4818-99ac-b00a03d6accd"


def make_config(tmp_path: Path) -> OperatorConfiguration:
    artifact_root = tmp_path / "artifacts"
    return OperatorConfiguration(
        repository_root=tmp_path,
        operational_database="pados_local_runtime",
        corpus_database="pados_corpus_runtime",
        database_host="127.0.0.1",
        database_port=5432,
        database_user="pados",
        database_password="test-only",
        artifact_root=artifact_root,
        staging_root=artifact_root / "staging",
        operator_name="test-operator",
    )


def test_configuration_rejects_shared_operational_and_corpus_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADOS_CORPUS_DATABASE_NAME", "pados_corpus_runtime")
    monkeypatch.setenv("PADOS_CORPUS_OPERATIONAL_DATABASE", "pados_corpus_runtime")
    with pytest.raises(OperatorConfigurationError, match="distinct"):
        resolve_operator_configuration(Settings())


def test_batch_requires_human_authorization_and_binds_manifest_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    monkeypatch.setattr(operator, "configuration", lambda: config)
    monkeypatch.setattr(operator, "_operational_project", lambda _config, _project_id: {"id": PROJECT_ID})
    monkeypatch.setattr(operator, "_known_hashes", lambda _config, _project_id: set())

    staged = operator.stage_batch(
        PROJECT_ID,
        [operator.StagedUpload("paper.pdf", "application/pdf", b"%PDF-1.7 fixture")],
    )
    batch_id = str(staged["batch"]["batch_id"])
    scanned = operator.scan_batch(batch_id)
    reviewed = operator.review_batch(batch_id, ["paper.pdf"])
    assert scanned["batch"]["status"] == "SCANNED"
    assert reviewed["batch"]["status"] == "PENDING_HUMAN_AUTHORIZATION"

    with pytest.raises(operator.OperatorError) as unauthorized:
        operator.ingest_batch(batch_id)
    assert unauthorized.value.code == "UNAUTHORIZED_INGEST"
    assert unauthorized.value.write_occurred is False

    authorized = operator.authorize_batch(
        batch_id,
        manifest_sha256=str(reviewed["manifest_sha256"]),
        selected_file_hashes=dict(reviewed["batch"]["selected_file_hashes"]),
    )
    assert authorized["batch"]["status"] == "AUTHORIZED"
    assert authorized["authorization"]["database_target"] == "pados_corpus_runtime"
    assert authorized["authorization"]["allow_operational_migration"] is False

    with pytest.raises(operator.OperatorError) as changed:
        operator.authorize_batch(
            batch_id,
            manifest_sha256="0" * 64,
            selected_file_hashes=dict(reviewed["batch"]["selected_file_hashes"]),
        )
    assert changed.value.code == "HUMAN_AUTHORIZATION_REQUIRED"


def test_batch_rejects_path_mime_and_size_violations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    small_config = OperatorConfiguration(**{**config.__dict__, "max_file_bytes": 4})
    monkeypatch.setattr(operator, "configuration", lambda: small_config)
    monkeypatch.setattr(operator, "_operational_project", lambda _config, _project_id: {"id": PROJECT_ID})

    with pytest.raises(operator.OperatorError) as path_error:
        operator.stage_batch(PROJECT_ID, [operator.StagedUpload("../escape.pdf", "application/pdf", b"ok")])
    assert path_error.value.code == "UNSAFE_FILENAME"

    with pytest.raises(operator.OperatorError) as mime_error:
        operator.stage_batch(PROJECT_ID, [operator.StagedUpload("paper.pdf", "text/plain", b"ok")])
    assert mime_error.value.code == "UNSUPPORTED_MIME_TYPE"

    with pytest.raises(operator.OperatorError) as size_error:
        operator.stage_batch(PROJECT_ID, [operator.StagedUpload("paper.pdf", "application/pdf", b"12345")])
    assert size_error.value.code == "FILE_SIZE_LIMIT_EXCEEDED"


def test_expired_receipt_is_rejected_without_ingest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    monkeypatch.setattr(operator, "configuration", lambda: config)
    monkeypatch.setattr(operator, "_operational_project", lambda _config, _project_id: {"id": PROJECT_ID})
    staged = operator.stage_batch(
        PROJECT_ID,
        [operator.StagedUpload("paper.pdf", "application/pdf", b"%PDF-1.7 fixture")],
    )
    batch_id = str(staged["batch"]["batch_id"])
    monkeypatch.setattr(operator, "_known_hashes", lambda _config, _project_id: set())
    operator.scan_batch(batch_id)
    operator.review_batch(batch_id)
    reviewed = operator._load_manifest(config, batch_id)
    operator.authorize_batch(
        batch_id,
        manifest_sha256=operator._manifest_hash(config, batch_id),
        selected_file_hashes=dict(reviewed["selected_file_hashes"]),
    )
    receipt_path = operator._receipt_path(config, batch_id)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["expires_at"] = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)).isoformat()
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    manifest = operator._load_manifest(config, batch_id)
    with pytest.raises(operator.OperatorError) as expired:
        operator._verified_receipt(config, manifest)
    assert expired.value.code == "AUTHORIZATION_EXPIRED"


def test_published_batch_is_idempotent_on_safe_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_config(tmp_path)
    batch_id = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(operator, "configuration", lambda: config)
    operator._write_json(
        operator._batch_path(config, batch_id),
        {"batch_id": batch_id, "project_id": PROJECT_ID, "database_target": config.corpus_database, "status": "PUBLISHED"},
    )
    result = operator.ingest_batch(batch_id)
    assert result == {"status": "ok", "batch_id": batch_id, "state": "PUBLISHED", "idempotency": "SAFE_RESUME_ALREADY_PUBLISHED"}
