# SPDX-License-Identifier: AGPL-3.0-only
"""Installation doctor: PASS / WARNING / BLOCKED per area, composed from existing PADOS health functions.

No new health authority: database reachability is ``pados.db.check_database``, lane compatibility uses Core
``Settings`` revisions and the runtime's Alembic graph, readiness is ``pados.operations.service.system_health``
fed through ``pados.execution.readiness.readiness``, the security section is ``security_posture``, device and
runtime facts come from the Device Profiler and the loopback-only runtime discovery, and storage pressure is
``storage_flexibility.pressure_of``. Output carries codes and safe values only (never credentials).
"""
from __future__ import annotations

import os
import shutil
import socket
import stat
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pados.install import database
from pados.install.envfile import ConfigFileError, findings, read_config
from pados.install.home import CONTROL_TOKEN_FILE, config_file, storage_profile_file
from pados.install.storage_profile import load_profile

DOCTOR_SCHEMA = "pados.install.doctor.v1"


class Level(StrEnum):
    PASS = "PASS"
    WARNING = "WARNING"
    BLOCKED = "BLOCKED"


def finding(area: str, level: Level, code: str, summary: str, action: str | None = None,
            **details: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"area": area, "status": level.value, "code": code, "summary": summary}
    if action:
        item["action"] = action
    if details:
        item["details"] = details
    return item


def _owner_only(path: Path) -> bool | None:
    if os.name == "nt":
        return None  # ACLs are set by the installer (icacls); POSIX mode bits do not apply.
    return stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def check_configuration(home: Path) -> tuple[dict[str, Any], Any]:
    from pados.config import Settings, get_settings
    from pados.control.security import assert_localhost_binding

    path = config_file(home)
    try:
        values = read_config(path)
    except ConfigFileError as exc:
        action = "pados init" if exc.code == "CONFIG_MISSING" else f"fix {path} (see docs/install/TROUBLESHOOTING.md)"
        return finding("configuration", Level.BLOCKED, exc.code, "PADOS configuration is missing or unreadable.",
                       action, line=exc.line), None
    try:
        get_settings.cache_clear()
        settings: Settings = get_settings()
        assert_localhost_binding(settings)
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors() if error.get("loc")})
        return finding("configuration", Level.BLOCKED, "CONFIG_INVALID", "PADOS configuration has invalid values.",
                       f"fix {path}", fields=fields), None
    except PermissionError:
        return finding("configuration", Level.BLOCKED, "API_NOT_LOOPBACK",
                       "The API must bind to 127.0.0.1 or localhost.", f"set PADOS_API_HOST=127.0.0.1 in {path}"), None
    found = findings(values)
    if not found.password_ref_valid:
        return finding("configuration", Level.BLOCKED, "SECRET_REF_INVALID",
                       "PADOS_DB_PASSWORD_REF is not a valid SecretRef.",
                       "use secretref://env/<NAME> or secretref://libpq/passfile"), settings
    if found.secrets_in_file:
        return finding("configuration", Level.WARNING, "SECRET_IN_CONFIG",
                       "A credential is stored in the configuration file.",
                       "remove it and use PADOS_DB_PASSWORD_REF", keys=list(found.secrets_in_file)), settings
    return finding("configuration", Level.PASS, "CONFIG_OK", "Configuration is valid.",
                   database_url=settings.safe_database_url, api=f"{settings.api_host}:{settings.api_port}",
                   password_ref=found.password_ref or "none"), settings


def check_database(settings: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    report = database.check(settings)
    if report.get("error") == "DATABASE_DRIVER_UNAVAILABLE":
        blocked = finding("database", Level.BLOCKED, "DATABASE_DRIVER_UNAVAILABLE",
                          "The PostgreSQL client library (libpq) was not found.",
                          "install your distribution's libpq package (Debian/Ubuntu: libpq5; Fedora/RHEL: libpq), "
                          "then: pados doctor")
        return blocked, finding("migrations", Level.WARNING, "NOT_ASSESSED", "Migrations cannot be checked."), report
    if report.get("error"):
        blocked = finding("database", Level.BLOCKED, str(report["error"]), "Database targets are not allowed.",
                          "choose dedicated database names (pados init --db-name ...)")
        return blocked, finding("migrations", Level.BLOCKED, "NOT_ASSESSED", "Database targets are not allowed."), report
    states = {lane["lane"]: lane["state"] for lane in report["lanes"]}
    managed = [lane for lane in report["lanes"] if lane["state"] != database.LaneState.NOT_MANAGED.value]
    blocking = {
        database.LaneState.UNREACHABLE.value: ("DATABASE_UNREACHABLE", "PostgreSQL is not reachable.",
                                               "start PostgreSQL, then run: pados doctor"),
        database.LaneState.AUTHENTICATION_FAILED.value: (
            "DATABASE_AUTHENTICATION_FAILED", "PostgreSQL rejected the credentials.",
            "set the variable named by PADOS_DB_PASSWORD_REF (or your libpq passfile), then: pados doctor"),
        database.LaneState.DATABASE_MISSING.value: (
            "DATABASE_MISSING", "A PADOS database does not exist yet.",
            "create it, or run: pados init --create-databases"),
    }
    for lane in managed:
        if lane["state"] in blocking:
            code, summary, action = blocking[lane["state"]]
            return (finding("database", Level.BLOCKED, code, summary, action, lanes=states),
                    finding("migrations", Level.WARNING, "NOT_ASSESSED", "Migrations cannot be checked."), report)
    db_item = finding("database", Level.PASS, "DATABASE_REACHABLE", "PostgreSQL is reachable.", lanes=states)
    if report["status"] == "CURRENT":
        migrations = finding("migrations", Level.PASS, "MIGRATIONS_CURRENT", "Schema matches this runtime.",
                             lanes={lane["lane"]: lane["revisions"] for lane in managed})
    elif any(lane["state"] == database.LaneState.INCOMPATIBLE.value for lane in managed):
        migrations = finding("migrations", Level.BLOCKED, "MIGRATIONS_INCOMPATIBLE",
                             "The database schema is newer than or foreign to this runtime.",
                             "use the matching PADOS version (pados update --rollback) or restore a backup",
                             lanes=states)
    else:
        migrations = finding("migrations", Level.BLOCKED, "MIGRATIONS_PENDING", "Database migrations are pending.",
                             "pados init (or pados update) applies them", lanes=states)
    return db_item, migrations, report


def check_storage(home: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    from pados.lifecycle.storage_flexibility import StorageObservation, StoragePressure, pressure_of

    path = storage_profile_file(home)
    try:
        mode, budget = load_profile(path)
    except (OSError, ValueError, KeyError) as exc:
        code = "STORAGE_PROFILE_MISSING" if isinstance(exc, FileNotFoundError) else "STORAGE_PROFILE_INVALID"
        blocked = finding("storage", Level.BLOCKED, code, "Storage profile is missing or invalid.", "pados init")
        return blocked, finding("disk_pressure", Level.WARNING, "NOT_ASSESSED", "No storage budget configured.")
    data = home / "data"
    writable = data.is_dir() and os.access(data, os.W_OK)
    storage = finding("storage", Level.PASS if writable else Level.BLOCKED,
                      "STORAGE_OK" if writable else "DATA_DIRECTORY_UNUSABLE",
                      f"Storage mode {mode.value}; model weights stay with their runtime (by reference).",
                      None if writable else f"make {data} writable", mode=mode.value,
                      budget=budget.model_dump(mode="json"))
    usage = shutil.disk_usage(home)
    pressure = pressure_of(StorageObservation(total_bytes=usage.total, free_bytes=usage.free), budget)
    level = {StoragePressure.NORMAL: Level.PASS, StoragePressure.PRESSURED: Level.WARNING,
             StoragePressure.CRITICAL: Level.BLOCKED}[pressure]
    disk = finding("disk_pressure", level, f"STORAGE_{pressure.value}",
                   f"Free space {usage.free // 2**20} MiB with reserve {budget.min_free_bytes // 2**20} MiB.",
                   None if level is Level.PASS else "free disk space or lower the reserve (pados init --min-free-bytes)",
                   free_bytes=usage.free, total_bytes=usage.total, reserve_bytes=budget.min_free_bytes)
    return storage, disk


def check_device_and_runtimes(home: Path, settings: Any) -> list[dict[str, Any]]:
    from pados.local_intelligence.device import DeviceProfiler, GpuStatus
    from pados.local_intelligence.runtimes import default_runtime_endpoints, describe_runtime

    now = datetime.now(UTC)
    runtimes = tuple(describe_runtime(endpoint, now=now)
                     for endpoint in default_runtime_endpoints(settings.ollama_base_url))
    profile = DeviceProfiler.local(disk_path=str(home)).profile(runtimes=runtimes)
    gpu_level = Level.WARNING if profile.gpu.status is GpuStatus.GPU_DISCOVERY_FAILED else Level.PASS
    device = finding("device", gpu_level, f"DEVICE_{profile.compute_class.value}",
                     f"{profile.os_name or profile.os} {profile.architecture}; GPU: {profile.gpu.status.value}.",
                     os=str(profile.os), architecture=str(profile.architecture),
                     logical_cores=profile.cpu.logical_cores,
                     memory_total_mb=profile.memory.total_mb if profile.memory else None,
                     gpu_status=profile.gpu.status.value, cuda_available=profile.gpu.cuda_available,
                     network_mode=profile.network_mode.value)
    healthy = [item for item in runtimes if item.available]
    runtime = finding("runtime", Level.PASS if healthy else Level.WARNING,
                      "LOCAL_RUNTIME_AVAILABLE" if healthy else "NO_LOCAL_RUNTIME",
                      f"{len(healthy)} local model runtime(s) reachable on loopback." if healthy else
                      "No local model runtime detected (optional; PADOS never installs one).",
                      None if healthy else "optional: start a local runtime such as Ollama",
                      runtimes=[{"runtime_id": item.runtime_id, "health": item.health.value,
                                 "endpoint": item.endpoint} for item in runtimes])
    model_count = sum(len(item.models) for item in healthy)
    models = finding("models", Level.PASS if model_count else Level.WARNING,
                     "MODELS_DISCOVERED" if model_count else "NO_LOCAL_MODELS",
                     f"{model_count} model(s) listed by local runtimes." if model_count else
                     "No local models discovered (PADOS never downloads models).", model_count=model_count)
    return [device, runtime, models]


def check_network(settings: Any, port_probe: Callable[[str, int], bool]) -> dict[str, Any]:
    free = port_probe(settings.api_host, settings.api_port)
    return finding("network", Level.PASS if free else Level.WARNING, "PORT_FREE" if free else "PORT_IN_USE",
                   f"API endpoint {settings.api_host}:{settings.api_port} (loopback only; no cloud access)."
                   if free else f"Port {settings.api_port} is in use (PADOS may already be running).",
                   None if free else "pados status (or choose another port: pados init --api-port)")


def port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host if host != "localhost" else "127.0.0.1", port))
        except OSError:
            return False
    return True


def check_readiness(settings: Any, db_ok: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    from pados.execution.readiness import readiness
    from pados.operations.service import system_health

    if not db_ok:
        return (finding("providers", Level.WARNING, "NOT_ASSESSED", "Provider routing needs the database."),
                finding("readiness", Level.BLOCKED, "CORE_BLOCKED", "PADOS Core is not usable yet.", "pados doctor"))
    try:
        health = system_health(settings)
    except Exception as exc:  # noqa: BLE001 - readiness failure is reported as data
        return (finding("providers", Level.WARNING, "NOT_ASSESSED", type(exc).__name__),
                finding("readiness", Level.BLOCKED, "SYSTEM_HEALTH_FAILED", "Readiness could not be assessed."))
    provider_ok = bool(health["dimensions"].get("provider_configured"))
    providers = finding("providers", Level.PASS if provider_ok else Level.WARNING,
                        "PROVIDER_CONFIGURED" if provider_ok else "PROVIDER_UNAVAILABLE",
                        "An execution provider is selected by the Worker Router." if provider_ok else
                        "No execution provider is configured (Core runs; product loop execution is unavailable).",
                        chat_provider=settings.chat_provider, embed_provider=settings.embed_provider)
    state = readiness({"runtime": {"status": "ok"}, "files": {"alembic_ini": True, "migrations": True},
                       "database": {"status": "ok"}, "status": "ok", "system_health": health})
    level = {"ready": Level.PASS, "degraded": Level.WARNING}.get(str(state["state"]), Level.BLOCKED)
    return providers, finding("readiness", level, f"CORE_{str(state['state']).upper()}",
                              "PADOS Core is ready." if level is Level.PASS else
                              "PADOS Core runs; full product-loop readiness is pending (see reasons).",
                              reasons=list(health.get("reasons", [])))


def check_measured_binding(settings: Any, providers: dict[str, Any]) -> dict[str, Any]:
    """For a local chat model, the providers area reports when the measured execution binding refuses (the same
    resolver the consumer path uses; it observes the device and the loopback runtime, and never calls a model)."""
    from pados.providers.execution_binding import (
        ExecutionBindingRefused,
        ExecutionIntent,
        resolve_bound_execution,
    )

    if str(settings.chat_provider).casefold() != "ollama" or not settings.chat_model:
        return providers
    try:
        resolve_bound_execution(settings, ExecutionIntent.STRUCTURED_OUTPUT, project_id="*")
    except ExecutionBindingRefused as exc:
        return finding("providers", Level.WARNING, "MEASURED_BINDING_UNAVAILABLE",
                       f"Local chat model calls are refused: {exc.refusal.code}.",
                       "import an accepted measured profile into PADOS_MODEL_CATALOG and authorize its model "
                       "reference in the compute-node registry",
                       refusal_code=exc.refusal.code, reasons=list(exc.refusal.reasons),
                       previous_code=providers.get("code"))
    except Exception as exc:  # noqa: BLE001 - doctor reports, never raises
        return finding("providers", Level.WARNING, "MEASURED_BINDING_UNAVAILABLE", type(exc).__name__,
                       previous_code=providers.get("code"))
    return providers


def check_security(home: Path, settings: Any) -> dict[str, Any]:
    from pados.security_posture import security_posture

    posture = security_posture(settings)
    problems = list(posture["warnings"])
    for path in (config_file(home), home / "config" / CONTROL_TOKEN_FILE):
        if path.exists() and _owner_only(path) is False:
            problems.append(f"PERMISSIONS_TOO_OPEN:{path.name}")
    return finding("security", Level.WARNING if problems else Level.PASS,
                   "SECURITY_WARNINGS" if problems else "SECURITY_OK",
                   "; ".join(problems) if problems else f"Profile {posture['deployment_profile']}; loopback only.",
                   "run pados init to regenerate owner-only files" if problems else None)


def run(home: Path, *, include_device: bool = True,
        port_probe: Callable[[str, int], bool] = port_is_free) -> dict[str, Any]:
    from pados.versions import version_report

    report = version_report()
    areas: list[dict[str, Any]] = [finding(
        "core", Level.PASS, "CORE_IMPORTED", f"PADOS {report['product']['version']} ({report['product']['channel']}).",
        python=sys.version.split()[0], app_root=str(database.app_root()))]
    config_item, settings = check_configuration(home)
    areas.append(config_item)
    db_ok = False
    if settings is None:
        areas += [finding(area, Level.WARNING, "NOT_ASSESSED", "Configuration is required first.")
                  for area in ("database", "migrations", "providers", "readiness", "security")]
    else:
        db_item, migrations, _ = check_database(settings)
        db_ok = db_item["status"] == Level.PASS.value
        areas += [db_item, migrations]
    areas += list(check_storage(home))
    if settings is not None:
        if include_device:
            areas += check_device_and_runtimes(home, settings)
        areas.append(check_network(settings, port_probe))
        providers, readiness_item = check_readiness(settings, db_ok)
        if include_device:  # the binding observes the device and runtime like the device area does
            providers = check_measured_binding(settings, providers)
        areas += [providers, readiness_item]
        areas.append(check_security(home, settings))
    statuses = {item["status"] for item in areas}
    overall = Level.BLOCKED if Level.BLOCKED.value in statuses else (
        Level.WARNING if Level.WARNING.value in statuses else Level.PASS)
    return {"schema": DOCTOR_SCHEMA, "status": overall.value, "home": str(home), "areas": areas,
            "version": report["product"]}
