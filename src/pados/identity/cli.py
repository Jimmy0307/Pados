# SPDX-License-Identifier: AGPL-3.0-only
"""``pados device pair|list|revoke`` (design §3.6): control-token calls to the loopback identity API.

The control token is read from the local token file and sent only in the ``X-PADOS-Control-Token`` header to a
loopback address. It is never printed. The only secret these commands print is the pairing code itself. Output is
ASCII (``--json`` is one JSON document on standard output), so Windows PowerShell 5.1 can parse it with
``ConvertFrom-Json``; refusals are one closed code on standard error with a non-zero exit code.

Token file, first match: ``--token-file``; ``<PADOS_IDENTITY_STORE_DIR>/../control.token`` (``pados init`` writes the
store as ``<dir>/runtime/local/identity`` beside ``control.token``); ``<PADOS_HOME>/config/control.token`` (installer).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, NoReturn
from urllib.parse import urlsplit

import httpx
import typer

from pados.config import get_settings
from pados.control.security import CONTROL_TOKEN_HEADER

device_app = typer.Typer(no_args_is_help=True, help="Browser device pairing and sessions (identity phase 1).")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_TIMEOUT_SECONDS = 15.0

BaseUrlOption = Annotated[str | None, typer.Option(
    "--base-url", help="Loopback API base URL (default: the configured PADOS_API_HOST and PADOS_API_PORT over http).")]
TokenFileOption = Annotated[Path | None, typer.Option(
    "--token-file", help="Control token file (default: beside the identity store, else PADOS_HOME/config).")]
JsonOption = Annotated[bool, typer.Option("--json", help="Print one JSON document.")]


def _fail(code: str) -> NoReturn:
    typer.echo(code, err=True)
    raise typer.Exit(code=1)


def client_factory(base_url: str) -> httpx.Client:
    """The HTTP client (tests substitute an in-process transport). Proxy settings from the environment are ignored."""
    return httpx.Client(base_url=base_url, timeout=_TIMEOUT_SECONDS, trust_env=False)


def _base_url(explicit: str | None) -> str:
    if explicit:
        base = explicit.rstrip("/")
    else:
        settings = get_settings()
        host = f"[{settings.api_host}]" if ":" in settings.api_host else settings.api_host
        base = f"http://{host}:{settings.api_port}"
    parts = urlsplit(base)
    if parts.scheme not in {"http", "https"} or (parts.hostname or "") not in _LOOPBACK_HOSTS or parts.path:
        _fail("BASE_URL_NOT_LOOPBACK")
    return base


def _token_file(explicit: Path | None) -> Path:
    if explicit is not None:
        return explicit
    candidates = [Path(get_settings().identity_store_dir).expanduser().resolve().parent / "control.token"]
    try:
        from pados.install.home import CONTROL_TOKEN_FILE, resolve_home

        candidates.append(resolve_home() / "config" / CONTROL_TOKEN_FILE)
    except ValueError:  # an invalid PADOS_HOME only removes that candidate
        pass
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    _fail("CONTROL_TOKEN_FILE_NOT_FOUND")


def _control_value(explicit: Path | None) -> str:
    try:
        value = _token_file(explicit).read_text(encoding="utf-8").strip()
    except OSError:
        _fail("CONTROL_TOKEN_FILE_UNREADABLE")
    if not value:
        _fail("CONTROL_TOKEN_FILE_EMPTY")
    return value


def _refusal_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP_{response.status_code}"
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return str(code) if isinstance(code, str) and code.isascii() else f"HTTP_{response.status_code}"


def _call(method: str, path: str, *, base_url: str | None, token_file: Path | None,
          body: dict[str, Any] | None = None) -> dict[str, Any]:
    base = _base_url(base_url)
    headers = {CONTROL_TOKEN_HEADER: _control_value(token_file), "Accept": "application/json"}
    try:
        with client_factory(base) as client:
            response = client.request(method, path, headers=headers, json=body)
    except httpx.HTTPError:
        _fail("API_UNREACHABLE")
    if response.status_code >= 400:
        _fail(_refusal_code(response))
    payload = response.json()
    if not isinstance(payload, dict):
        _fail("UNEXPECTED_RESPONSE")
    return payload


def _echo_json(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True))


@device_app.command("pair")
def device_pair(
    label: Annotated[str | None, typer.Option("--label", help="Default label for the paired browser.")] = None,
    json_output: JsonOption = False,
    base_url: BaseUrlOption = None,
    token_file: TokenFileOption = None,
) -> None:
    """Mint a one-time pairing code (valid 5 minutes) to type into the Control Center."""
    body = {"label": label} if label else {}
    payload = _call("POST", "/identity/v1/pairing-codes", base_url=base_url, token_file=token_file, body=body)
    result = {"pairing_code": str(payload["pairing_code"]), "expires_at": str(payload["expires_at"])}
    if json_output:
        _echo_json(result)
        return
    typer.echo(f"Pairing code: {result['pairing_code']}")
    typer.echo(f"Expires at:   {result['expires_at']}")
    typer.echo("In the Control Center choose 'Pair this browser' and type the code. It works once.")


@device_app.command("list")
def device_list(json_output: JsonOption = False, base_url: BaseUrlOption = None,
                token_file: TokenFileOption = None) -> None:
    """Paired devices (no secrets)."""
    payload = _call("GET", "/identity/v1/devices", base_url=base_url, token_file=token_file)
    devices = [dict(item) for item in payload.get("devices", []) if isinstance(item, dict)]
    if json_output:
        _echo_json({"devices": devices})
        return
    if not devices:
        typer.echo("No paired devices.")
    for item in devices:
        state = "revoked" if item.get("revoked_at") else "active"
        label = str(item.get("device_label", "")).encode("ascii", "replace").decode("ascii")
        typer.echo(f"{item.get('device_id')} {state} paired_at={item.get('paired_at')} "
                   f"last_seen_at={item.get('last_seen_at')} label={label}")


@device_app.command("revoke")
def device_revoke(
    device_id: Annotated[str, typer.Argument(help="The device id (dev_...) from `pados device list`.")],
    base_url: BaseUrlOption = None,
    token_file: TokenFileOption = None,
) -> None:
    """Revoke a device and every session it holds."""
    if not re.fullmatch(r"dev_[0-9a-f]{16}", device_id):
        _fail("DEVICE_NOT_FOUND")
    payload = _call("POST", f"/identity/v1/devices/{device_id}/revoke", base_url=base_url, token_file=token_file)
    typer.echo(f"revoked device_id={payload.get('device_id')} revoked_at={payload.get('revoked_at')}")
