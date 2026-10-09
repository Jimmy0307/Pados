"""npm bootstrap glue (``pados.install``): home, config loading, storage profile, lanes, doctor, CLI boundary."""
from __future__ import annotations

import json
import os
import socket
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from pados.config import get_settings
from pados.install import cli, database, doctor
from pados.install.envfile import (
    ConfigFileError,
    apply_config,
    bootstrap,
    findings,
    parse_env_text,
)
from pados.install.home import HomeError, config_file, default_home, ensure_layout, storage_profile_file
from pados.install.storage_profile import StorageMode, budget_for_mode, load_profile, write_profile

ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads((ROOT / "packages" / "cli" / "test" / "fixtures" / "home-vectors.json").read_text("utf-8"))
SECRET_MARKER = "pw-marker-never-printed-7f3a"


@pytest.fixture
def clean_env() -> Iterator[None]:
    saved = dict(os.environ)
    for key in list(os.environ):
        if key.startswith("PADOS_") or key in {"PGPASSFILE", "PGPASSWORD"}:
            del os.environ[key]
    get_settings.cache_clear()
    yield
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_config(home: Path, **overrides: str) -> Path:
    ensure_layout(home)
    values = {
        "PADOS_ENV": "local", "PADOS_API_HOST": "127.0.0.1", "PADOS_API_PORT": str(_closed_port()),
        "PADOS_DB_HOST": "127.0.0.1", "PADOS_DB_PORT": str(_closed_port()), "PADOS_DB_USER": "pados",
        "PADOS_DB_PASSWORD_REF": "secretref://env/PADOS_TEST_DB_SECRET",
        "PADOS_DB_NAME": "pados_app_test", "PADOS_OPERATIONAL_DATABASE_NAME": "pados_app_test",
        "PADOS_CORPUS_DATABASE_NAME": "pados_app_test_corpus",
        "PADOS_PRODUCT_WORKSPACE_ROOT": str(home / "data" / "workspace"),
        "PADOS_PRODUCT_OBJECT_ROOT": str(home / "data" / "objects"),
        **overrides,
    }
    path = config_file(home)
    path.write_text("# test\n" + "\n".join(f"{k}={v}" for k, v in values.items()) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=[v["name"] for v in VECTORS["vectors"]])
def test_home_vectors_match_node_resolver(vector: dict[str, object]) -> None:
    env = dict(vector["env"])  # type: ignore[call-overload]
    if "error" in vector:
        with pytest.raises(HomeError) as raised:
            default_home(str(vector["platform"]), env, str(vector["user_home"]))
        assert raised.value.code == vector["error"]
    else:
        assert default_home(str(vector["platform"]), env, str(vector["user_home"])) == vector["expect"]


def test_env_parsing_is_literal_and_strict() -> None:
    values = parse_env_text("# c\nPADOS_A=1\nPADOS_B=\"two words\"\n\nPADOS_C=$HOME\n")
    assert values == {"PADOS_A": "1", "PADOS_B": "two words", "PADOS_C": "$HOME"}
    with pytest.raises(ConfigFileError) as invalid:
        parse_env_text("not a line\n")
    assert invalid.value.code == "CONFIG_LINE_INVALID" and invalid.value.line == 1
    with pytest.raises(ConfigFileError) as duplicate:
        parse_env_text("PADOS_A=1\nPADOS_A=2\n")
    assert duplicate.value.code == "CONFIG_KEY_DUPLICATED"


def test_environment_wins_and_secret_ref_resolves_in_memory_only() -> None:
    environ = {"PADOS_API_PORT": "9999", "MY_DB_SECRET": SECRET_MARKER}
    applied = apply_config({"PADOS_API_PORT": "8088", "PADOS_DB_PASSWORD_REF": "secretref://env/MY_DB_SECRET"},
                           environ)
    assert environ["PADOS_API_PORT"] == "9999" and "PADOS_API_PORT" not in applied
    assert environ["PADOS_DB_PASSWORD"] == SECRET_MARKER


def test_missing_secret_never_falls_back_to_core_development_default() -> None:
    environ: dict[str, str] = {}
    apply_config({"PADOS_DB_PASSWORD_REF": "secretref://env/ABSENT_SECRET"}, environ)
    assert environ["PADOS_DB_PASSWORD"] == ""
    passfile_env: dict[str, str] = {}
    apply_config({"PADOS_DB_PASSWORD_REF": "secretref://libpq/passfile", "PADOS_DB_PASSFILE": "/x/pgpass"},
                 passfile_env)
    assert passfile_env["PADOS_DB_PASSWORD"] == "" and passfile_env["PGPASSFILE"] == "/x/pgpass"


def test_findings_flag_stored_credentials_and_invalid_refs() -> None:
    found = findings({"PADOS_DB_PASSWORD": "x", "PADOS_DB_PASSWORD_REF": "plain-text"})
    assert found.secrets_in_file == ("PADOS_DB_PASSWORD",) and not found.password_ref_valid
    clean = findings({"PADOS_DB_PASSWORD": "", "PADOS_DB_PASSWORD_REF": "secretref://env/X"})
    assert clean.secrets_in_file == () and clean.password_ref_valid


def test_storage_modes_map_onto_storage_budget(tmp_path: Path) -> None:
    light = budget_for_mode(StorageMode.LIGHTWEIGHT)
    full = budget_for_mode(StorageMode.FULL_LOCAL)
    assert light.max_local_residency_bytes is not None and full.max_local_residency_bytes is None
    assert light.min_free_bytes < budget_for_mode(StorageMode.STANDARD).min_free_bytes < full.min_free_bytes
    document = write_profile(tmp_path / "p.json", StorageMode.LIGHTWEIGHT, min_free_bytes=123)
    assert document["model_weights"] == "EXTERNAL_REFERENCE" and document["authority"] == "NONE"
    mode, budget = load_profile(tmp_path / "p.json")
    assert mode is StorageMode.LIGHTWEIGHT and budget.min_free_bytes == 123


def test_lane_status_classification(clean_env: None) -> None:
    settings = get_settings().model_copy(update={"db_name": "pados_app_x", "operational_database_name": "pados_app_x",
                                                 "corpus_database_name": "pados_app_x_corpus"})
    lane = database.managed_lanes(settings)[0]
    assert lane.target_revision == settings.operational_revision

    def status(revisions: tuple[str, ...] | None) -> database.LaneState:
        original = database._read_revisions
        database._read_revisions = lambda _settings: revisions  # type: ignore[assignment]
        try:
            return database.lane_status(settings, lane, ancestors=lambda _t: frozenset({"a", "b"})).state
        finally:
            database._read_revisions = original  # type: ignore[assignment]

    assert status(None) is database.LaneState.UNINITIALIZED
    assert status((lane.target_revision,)) is database.LaneState.CURRENT
    assert status(("a",)) is database.LaneState.UPGRADE_AVAILABLE
    assert status(("zzz_newer",)) is database.LaneState.INCOMPATIBLE
    assert status(("a", "b")) is database.LaneState.INCOMPATIBLE


def test_connection_error_classification() -> None:
    class Missing(Exception):
        sqlstate = "3D000"

    class Auth(Exception):
        sqlstate = "28P01"

    assert database.classify_connection_error(Missing()) is database.LaneState.DATABASE_MISSING
    assert database.classify_connection_error(Auth()) is database.LaneState.AUTHENTICATION_FAILED
    assert database.classify_connection_error(OSError("refused")) is database.LaneState.UNREACHABLE


def test_target_rules_refuse_protected_and_invalid_names(clean_env: None) -> None:
    settings = get_settings()
    protected = settings.model_copy(update={"db_name": "pados_local_runtime",
                                            "operational_database_name": "pados_local_runtime"})
    assert database.target_error(protected) == "PROTECTED_DATABASE_TARGET"
    invalid = settings.model_copy(update={"db_name": "x; drop", "operational_database_name": "x; drop"})
    assert database.target_error(invalid) == "DATABASE_NAME_INVALID"


def test_alembic_graph_ancestry_from_runtime_migrations() -> None:
    ancestors = database.ancestors_of("20261007_0017", ROOT)
    assert "20260710_0001" in ancestors and "20260730_0011" not in ancestors


def test_doctor_reports_unreachable_database_without_secrets(tmp_path: Path, clean_env: None) -> None:
    home = tmp_path / "home"
    _write_config(home)
    write_profile(storage_profile_file(home), StorageMode.LIGHTWEIGHT, min_free_bytes=0)
    os.environ["PADOS_TEST_DB_SECRET"] = SECRET_MARKER
    bootstrap(home)
    report = doctor.run(home, include_device=False)
    by_area = {item["area"]: item for item in report["areas"]}
    assert report["status"] == "BLOCKED"
    assert by_area["configuration"]["status"] == "PASS"
    assert by_area["database"]["code"] == "DATABASE_UNREACHABLE"
    assert by_area["storage"]["status"] == "PASS" and by_area["disk_pressure"]["code"].startswith("STORAGE_")
    assert SECRET_MARKER not in json.dumps(report)


def test_doctor_blocks_on_insufficient_disk_reserve(tmp_path: Path, clean_env: None) -> None:
    home = tmp_path / "home"
    _write_config(home)
    write_profile(storage_profile_file(home), StorageMode.LIGHTWEIGHT, min_free_bytes=2**62)
    bootstrap(home)
    by_area = {item["area"]: item for item in doctor.run(home, include_device=False)["areas"]}
    assert by_area["disk_pressure"]["status"] == "BLOCKED"
    assert by_area["disk_pressure"]["code"] == "STORAGE_CRITICAL"


def test_doctor_blocks_on_invalid_or_missing_config(tmp_path: Path, clean_env: None) -> None:
    home = tmp_path / "home"
    ensure_layout(home)
    missing = {item["area"]: item for item in doctor.run(home, include_device=False)["areas"]}
    assert missing["configuration"]["code"] == "CONFIG_MISSING"
    _write_config(home, PADOS_API_PORT="not-a-port")
    bootstrap(home)
    invalid = {item["area"]: item for item in doctor.run(home, include_device=False)["areas"]}
    assert invalid["configuration"]["code"] == "CONFIG_INVALID"
    assert invalid["configuration"]["details"]["fields"] == ["PADOS_API_PORT"]


def test_cli_boundary_returns_coded_json_without_traceback(tmp_path: Path, clean_env: None,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["migrate", "--home", str(tmp_path / "empty"), "--check"]) == cli.EXIT_ERROR
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {"ok": False, "code": "CONFIG_MISSING",
                       "message": "PADOS configuration is missing or invalid; run `pados init`."}
    assert "Traceback" not in captured.err
    assert cli.main(["storage-profile", "--home", str(tmp_path / "h"), "--mode", "HUGE"]) == cli.EXIT_ERROR
    assert json.loads(capsys.readouterr().out)["code"] == "STORAGE_MODE_INVALID"


def test_cli_migrate_check_reports_unreachable_lanes(tmp_path: Path, clean_env: None,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    _write_config(home)
    assert cli.main(["migrate", "--home", str(home), "--check"]) == cli.EXIT_BLOCKED
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "BLOCKED"
    assert {lane["state"] for lane in payload["lanes"]} == {"UNREACHABLE"}


def test_missing_libpq_is_reported_as_a_driver_problem(tmp_path: Path, clean_env: None,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    _write_config(home)
    write_profile(storage_profile_file(home), StorageMode.LIGHTWEIGHT, min_free_bytes=0)
    bootstrap(home)
    monkeypatch.setitem(sys.modules, "psycopg", None)  # import fails exactly like a missing libpq
    assert database.check(get_settings())["error"] == "DATABASE_DRIVER_UNAVAILABLE"
    by_area = {item["area"]: item for item in doctor.run(home, include_device=False)["areas"]}
    assert by_area["database"]["code"] == "DATABASE_DRIVER_UNAVAILABLE"
    assert "libpq5" in by_area["database"]["action"]
