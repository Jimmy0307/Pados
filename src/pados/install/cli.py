# SPDX-License-Identifier: AGPL-3.0-only
"""``python -m pados.install <command>``: the machine interface the npm CLI shells out to.

Every command prints one JSON document on stdout. Failures print ``{"ok": false, "code": ..., "message": ...}``
and exit 2; tracebacks appear only with ``--debug`` or ``PADOS_DEBUG=1``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from pados.install.home import HomeError, ensure_layout, resolve_home, storage_profile_file

EXIT_BLOCKED = 1
EXIT_ERROR = 2


class InstallCommandError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def _home(value: str | None) -> Path:
    environ = dict(os.environ)
    if value:
        environ["PADOS_HOME"] = value
    return resolve_home(environ)


def _bootstrapped(args: argparse.Namespace) -> tuple[Path, Any]:
    from pados.config import get_settings
    from pados.install.envfile import ConfigFileError, bootstrap

    home = _home(args.home)
    try:
        bootstrap(home)
    except ConfigFileError as exc:
        raise InstallCommandError(exc.code, "PADOS configuration is missing or invalid; run `pados init`.") from exc
    return home, get_settings()


def cmd_doctor(args: argparse.Namespace) -> int:
    from pados.config import get_settings
    from pados.install import doctor
    from pados.install.envfile import ConfigFileError, bootstrap

    home = _home(args.home)
    try:
        bootstrap(home)
    except ConfigFileError:
        get_settings.cache_clear()
    report = doctor.run(home, include_device=not args.no_device)
    _emit(report)
    return EXIT_BLOCKED if report["status"] == "BLOCKED" else 0


def cmd_migrate(args: argparse.Namespace) -> int:
    from pados.install import database

    _, settings = _bootstrapped(args)
    created: list[str] = []
    if args.create_databases:
        before = database.check(settings)
        if before.get("error"):
            raise InstallCommandError(str(before["error"]), "Database driver or targets are not usable.")
        if any(lane["state"] == database.LaneState.UNREACHABLE.value for lane in before["lanes"]):
            raise InstallCommandError("DATABASE_UNREACHABLE", "PostgreSQL is not reachable.")
        created = database.create_missing_databases(settings)
    result = database.check(settings) if args.check else database.upgrade(settings)
    result["created_databases"] = created
    _emit(result)
    return 0 if result["status"] == "CURRENT" or (args.check and result["status"] == "UPGRADE_REQUIRED") \
        else EXIT_BLOCKED


def cmd_storage_profile(args: argparse.Namespace) -> int:
    from pados.install.storage_profile import StorageMode, write_profile

    home = _home(args.home)
    ensure_layout(home)
    try:
        mode = StorageMode(args.mode)
    except ValueError as exc:
        raise InstallCommandError("STORAGE_MODE_INVALID", "Storage mode must be LIGHTWEIGHT, STANDARD or "
                                  "FULL_LOCAL.") from exc
    document = write_profile(storage_profile_file(home), mode, min_free_bytes=args.min_free_bytes)
    _emit({"ok": True, "path": str(storage_profile_file(home)), "profile": document})
    return 0


def cmd_validate_config(args: argparse.Namespace) -> int:
    from pados.install.doctor import check_configuration

    from pados.install.envfile import ConfigFileError, bootstrap

    home = _home(args.home)
    try:
        bootstrap(home)
    except ConfigFileError as exc:
        raise InstallCommandError(exc.code, str(exc)) from exc
    item, _ = check_configuration(home)
    _emit({"ok": item["status"] != "BLOCKED", "configuration": item})
    return EXIT_BLOCKED if item["status"] == "BLOCKED" else 0


def cmd_serve(args: argparse.Namespace) -> int:
    from pados.install.serve import run

    _bootstrapped(args)
    run(args.instance_id)
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    from pados.install.database import app_root
    from pados.versions import version_report

    manifest_path = app_root() / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else None
    _emit({"ok": True, "versions": version_report(), "runtime_manifest": manifest})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m pados.install", description=__doc__.splitlines()[0])
    parser.add_argument("--debug", action="store_true", help="show tracebacks")
    sub = parser.add_subparsers(dest="command", required=True)

    def command(name: str, handler: Callable[[argparse.Namespace], int]) -> argparse.ArgumentParser:
        item = sub.add_parser(name)
        item.add_argument("--home", default=None, help="PADOS_HOME (defaults to the platform location)")
        item.set_defaults(handler=handler)
        return item

    doctor = command("doctor", cmd_doctor)
    doctor.add_argument("--no-device", action="store_true", help="skip device and runtime probes")
    migrate = command("migrate", cmd_migrate)
    migrate.add_argument("--check", action="store_true", help="report compatibility only")
    migrate.add_argument("--create-databases", action="store_true", help="create missing lane databases")
    storage = command("storage-profile", cmd_storage_profile)
    storage.add_argument("--mode", required=True)
    storage.add_argument("--min-free-bytes", type=int, default=None)
    command("validate-config", cmd_validate_config)
    serve = command("serve", cmd_serve)
    serve.add_argument("--instance-id", required=True)
    command("version", cmd_version)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    debug = bool(args.debug) or os.environ.get("PADOS_DEBUG", "") not in {"", "0", "false"}
    try:
        return int(args.handler(args))
    except InstallCommandError as exc:
        _emit({"ok": False, "code": exc.code, "message": str(exc)})
    except HomeError as exc:
        _emit({"ok": False, "code": exc.code, "message": "PADOS_HOME is not usable."})
    except Exception as exc:  # noqa: BLE001 - the boundary turns every failure into a coded result
        _emit({"ok": False, "code": "INTERNAL_ERROR", "message": type(exc).__name__})
    if debug:
        traceback.print_exc()
    return EXIT_ERROR
