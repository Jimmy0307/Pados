# SPDX-License-Identifier: AGPL-3.0-only
"""Live identity E2E: browser pairing and server-side sessions as real processes over real HTTP (loopback only).

Validates docs/identity/UNIFIED_IDENTITY_SESSION_DESIGN.md against a real ``pados serve`` (identity flag on, idle
timeout 120 s, the minimum the settings allow) backed by a real PostgreSQL, and records every step:

  1. ``pados init`` in a fresh temporary root; a private loopback PostgreSQL (``--pg-bin``, trust auth) or an existing
     server (``--pg-host/--pg-port``, password from ``PADOS_DB_PASSWORD``); the documented lane migrations
  2. pairing codes minted by ``pados device pair --json`` (and, on Windows, through ``powershell.exe`` 5.1 with
     ``ConvertFrom-Json``); ``POST /identity/v1/pair`` with an allowlisted Origin; a wrong Origin and a reused code
     are refused; the cookie is HttpOnly, SameSite=Strict, Path=/, not Secure over loopback HTTP
  3. a reload (``GET /identity/v1/session``) restores the session from the cookie alone; so does a server restart
  4. a project (``pados project create``: the only creation path, a direct-database operator command; there is no
     HTTP route for it with either credential) and then, with ONLY the session cookie + ``X-PADOS-CSRF`` + Origin, a
     Work Request, a WORK_ROUTE approval, a task goal and plan, a TaskLoop to COMPLETED and a ResultCandidate
     waiting for approval (``local_dogfood_e2e.task_flow`` driven through the session)
  5. refusals with their closed codes: missing/wrong CSRF, missing/wrong Origin, ``Sec-Fetch-Site: cross-site``, a
     forged cookie, an invalid control token header next to a valid cookie, no credential at all; a session cannot
     mint pairing codes
  6. WebAuthn step-up cannot be bypassed: the session's ``POST /control/approvals/{id}/approve`` on the candidate's
     approval is refused with RESULT_ACCEPTANCE_REQUIRES_STEP_UP and the approval stays PENDING
  7. device revoke (through another device's session, and through ``pados device revoke``) kills the revoked
     device's session; logout; idle expiry after 120 s without use; control-token rotation (``pados init --force``)
     ends every session while pairing works again with the new token
  8. optional (``--playwright``): the live browser spec ``web/e2e/identity-live.spec.ts`` through a Vite dev server
  9. OIDC (phase 2) with a configured provider whose HTTPS issuer is unreachable: providers are listed, sign-in fails
     closed with closed codes. A live SSO sign-in is NOT_LIVE_VALIDATED here: discovery is HTTPS-only with the system
     trust store (``trust_env=False``), so a local fake issuer cannot be reached without weakening it; the flow is
     covered by tests/identity/oidc with a mocked transport
 10. the raw control token never appears in a response (body or header) or a log captured by the run; session ids,
     CSRF values and pairing codes never appear in a server log

Evidence: ``<out>/IDENTITY_E2E_RESULT.json`` (``pados.identity-e2e.v1``) plus per-step logs; secrets are never written
to it. Exit 0 only when no step FAILED.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import EllipsisType
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import local_dogfood_e2e as dogfood  # noqa: E402 - the shared helpers of the dogfood E2E (unchanged)

REPO = dogfood.REPO
CONTRACT = "pados.identity-e2e.v1"
IDLE_TIMEOUT_SECONDS = 120  # the minimum PADOS_IDENTITY_IDLE_TIMEOUT_SECONDS allows
CODE_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}$")
CROSS_ORIGIN = "http://evil.example"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / \
    "powershell.exe"
STEP_UP_CODE = "RESULT_ACCEPTANCE_REQUIRES_STEP_UP"


@dataclass
class Reply:
    status: int
    headers: list[tuple[str, str]]
    text: str

    @property
    def body(self) -> Any:
        try:
            return json.loads(self.text or "null")
        except ValueError:
            return self.text[:500]

    @property
    def code(self) -> str | None:
        """The closed code: ``detail`` when it is a string, else the ``pados.error.v1`` code."""
        body = self.body
        if not isinstance(body, dict):
            return None
        if isinstance(body.get("detail"), str):
            return str(body["detail"])
        error = body.get("error")
        return str(error.get("code")) if isinstance(error, dict) and error.get("code") else None

    def header(self, name: str) -> list[str]:
        return [value for key, value in self.headers if key.lower() == name.lower()]


class Http:
    """Plain loopback HTTP with full header control; every reply is kept in memory for the leak scan only."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.replies: list[Reply] = []

    def request(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None) -> Reply:
        sent = {"Accept": "application/json", **(headers or {})}
        data = None if body is None else json.dumps(body).encode("utf-8")
        if data is not None:
            sent["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=120)
        try:
            connection.request(method, path, body=data, headers=sent)
            response = connection.getresponse()
            reply = Reply(response.status, response.getheaders(), response.read().decode("utf-8", "replace"))
        finally:
            connection.close()
        self.replies.append(reply)
        return reply


@dataclass
class Browser:
    """One paired browser: the session cookie (as the browser would hold it) and the CSRF value from the API."""

    http: Http
    origin: str
    label: str
    cookie: str = field(default="", repr=False)
    csrf: str = field(default="", repr=False)
    device_id: str = ""

    def headers(self, method: str, *, origin: str | None | EllipsisType = ..., csrf: str | None | EllipsisType = ...,
                cookie: str | None | EllipsisType = ..., fetch_site: str | None = "same-origin") -> dict[str, str]:
        chosen_cookie = self.cookie if isinstance(cookie, EllipsisType) else cookie
        chosen_origin = self.origin if isinstance(origin, EllipsisType) else origin
        chosen_csrf = self.csrf if isinstance(csrf, EllipsisType) else csrf
        headers: dict[str, str] = {}
        if chosen_cookie:
            headers["Cookie"] = f"pados_session={chosen_cookie}"
        if fetch_site:
            headers["Sec-Fetch-Site"] = fetch_site
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            if chosen_origin:
                headers["Origin"] = chosen_origin
            if chosen_csrf:
                headers["X-PADOS-CSRF"] = chosen_csrf
        return headers

    def send(self, method: str, path: str, body: Any = None, **overrides: Any) -> Reply:
        extra = overrides.pop("extra", None) or {}
        return self.http.request(method, path, body, {**self.headers(method, **overrides), **extra})


class SessionApi:
    """``local_dogfood_e2e.Api``'s calling convention, authenticated by the browser session only (no token header)."""

    def __init__(self, browser: Browser) -> None:
        self.browser = browser

    def call(self, method: str, path: str, body: Any = None, *, control: Any = ...,
             headers: dict[str, str] | None = None) -> tuple[int, Any]:
        del control  # never sent: the session is the only credential
        reply = self.browser.send(method, path, body, extra=headers)
        return reply.status, reply.body


class TokenApi:
    """The control-token caller (the operator's CLI path), recorded for the leak scan."""

    def __init__(self, http_client: Http, control_value: str) -> None:
        self.http, self.control_value = http_client, control_value

    def call(self, method: str, path: str, body: Any = None, *, control: str | None | EllipsisType = ...,
             headers: dict[str, str] | None = None) -> tuple[int, Any]:
        presented = self.control_value if isinstance(control, EllipsisType) else control
        sent = dict(headers or {})
        if presented is not None:
            sent["X-PADOS-Control-Token"] = presented
        reply = self.http.request(method, path, body, sent)
        return reply.status, reply.body


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def note(run: dogfood.Run, name: str, status: str, **detail: Any) -> None:
    """A step that neither passed nor failed (SKIPPED, NOT_LIVE_VALIDATED), with the reason in its detail."""
    run.steps.append({"step": name, "status": status, "detail": detail})
    print(f"[{status}] {name}", flush=True)


def session_cookie(reply: Reply) -> tuple[str, str]:
    """The ``pados_session`` value and its full Set-Cookie line (the value is never recorded)."""
    for line in reply.header("set-cookie"):
        name, _, rest = line.partition("=")
        if name.strip() == "pados_session":
            return rest.split(";", 1)[0], line
    return "", ""


def cookie_attributes(line: str) -> dict[str, Any]:
    parts = [part.strip().lower() for part in line.split(";")[1:]]
    return {"httponly": "httponly" in parts, "secure": "secure" in parts, "samesite": next(
        (p.split("=", 1)[1] for p in parts if p.startswith("samesite=")), None), "path": next(
        (p.split("=", 1)[1] for p in parts if p.startswith("path=")), None), "domain": any(
        p.startswith("domain=") for p in parts)}


@dataclass
class Context:
    run: dogfood.Run
    env: dict[str, str]
    root: Path
    http: Http
    api_port: int
    web_origin: str
    control_value: str = field(repr=False)
    secrets_seen: dict[str, set[str]] = field(default_factory=lambda: {"control": set(), "session": set(),
                                                                         "csrf": set(), "code": set()})

    @property
    def token_api(self) -> TokenApi:
        return TokenApi(self.http, self.control_value)

    def remember(self, kind: str, value: str) -> None:
        if value:
            self.secrets_seen[kind].add(value)

    def browser(self, label: str) -> Browser:
        return Browser(self.http, self.web_origin, label)

    def cli(self, name: str, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
        return self.run.command(name, [sys.executable, "-m", "pados.cli.main", *args], self.env, check=check)

    def mint_code(self, name: str, label: str) -> str:
        """``pados device pair --json`` with the default token-file discovery (beside the identity store)."""
        result = self.cli(name, "device", "pair", "--json", "--label", label, check=True)
        payload, _ = json.JSONDecoder().raw_decode(result.stdout.lstrip())
        code = str(payload["pairing_code"])
        self.remember("code", code)
        return code

    def pair(self, browser: Browser, code: str, *, origin: str | None = None) -> Reply:
        reply = self.http.request("POST", "/identity/v1/pair", {"pairing_code": code, "device_label": browser.label},
                                  {"Origin": origin or browser.origin, "Sec-Fetch-Site": "same-origin"})
        if reply.status == 200:
            browser.cookie, _ = session_cookie(reply)
            body = reply.body if isinstance(reply.body, dict) else {}
            browser.csrf, browser.device_id = str(body.get("csrf_token", "")), str(body.get("device_id", ""))
            self.remember("session", browser.cookie)
            self.remember("csrf", browser.csrf)
        return reply


# --- phases -----------------------------------------------------------------------------------------------------------
def pairing(ctx: Context) -> Browser:
    run = ctx.run
    anonymous = ctx.http.request("GET", "/identity/v1/session")
    body = anonymous.body if isinstance(anonymous.body, dict) else {}
    run.record("identity_enabled_anonymous_session", anonymous.status == 200 and body.get("enabled") is True
               and body.get("authenticated") is False and body.get("providers") == []
               and "no-store" in ",".join(anonymous.header("cache-control")), http=anonymous.status, body=body)

    code = ctx.mint_code("device_pair_cli_json", "E2E browser A")
    cli_log = (run.out / "device_pair_cli_json.log").read_text(encoding="utf-8")
    run.record("pairing_code_cli_json", bool(CODE_PATTERN.fullmatch(code)) and ctx.control_value not in cli_log,
               format_ok=bool(CODE_PATTERN.fullmatch(code)), token_file="default discovery beside the identity store")
    ps_code = powershell_pairing(ctx)

    browser = ctx.browser("E2E browser A")
    wrong = ctx.pair(browser, code, origin=CROSS_ORIGIN)
    run.record("pair_refuses_wrong_origin", wrong.status == 403 and wrong.code == "ORIGIN_NOT_ALLOWED"
               and not session_cookie(wrong)[0], http=wrong.status, code=wrong.code)
    paired = ctx.pair(browser, code)
    _, line = session_cookie(paired)
    attributes = cookie_attributes(line)
    body = paired.body if isinstance(paired.body, dict) else {}
    run.record("pair_with_allowlisted_origin", paired.status == 200 and bool(browser.cookie) and bool(browser.csrf)
               and browser.device_id.startswith("dev_") and attributes == {
                   "httponly": True, "secure": False, "samesite": "strict", "path": "/", "domain": False}
               and browser.cookie not in paired.text and "no-store" in ",".join(paired.header("cache-control")),
               http=paired.status, cookie_attributes=attributes, device_id=browser.device_id,
               idle_timeout_seconds=body.get("idle_timeout_seconds"), body_keys=sorted(body))
    reused = ctx.pair(ctx.browser("E2E reuse"), code)
    run.record("pairing_code_single_use", reused.status == 403 and reused.code == "PAIRING_CODE_INVALID",
               http=reused.status, code=reused.code)
    if ps_code:
        other = ctx.browser("E2E browser via PowerShell code")
        used = ctx.pair(other, ps_code)
        run.record("pair_with_powershell_minted_code", used.status == 200 and bool(other.cookie), http=used.status)
    reload = browser.send("GET", "/identity/v1/session")
    restored = reload.body if isinstance(reload.body, dict) else {}
    run.record("reload_restores_session", reload.status == 200 and restored.get("authenticated") is True
               and restored.get("device_id") == browser.device_id and restored.get("csrf_token") == browser.csrf
               and restored.get("auth_method") == "device_pairing" and ctx.control_value not in reload.text,
               http=reload.status, device_id=restored.get("device_id"), auth_method=restored.get("auth_method"),
               idle_expires_at=restored.get("idle_expires_at"))
    return browser


def powershell_pairing(ctx: Context) -> str | None:
    """Windows PowerShell 5.1 parses ``pados device pair --json`` with ConvertFrom-Json (design §3.6)."""
    if os.name != "nt" or not POWERSHELL.is_file():
        note(ctx.run, "pairing_code_powershell51", "SKIPPED", reason="Windows PowerShell 5.1 exists only on Windows")
        return None
    python = sys.executable.replace("'", "''")
    script = (f"$raw = & '{python}' -m pados.cli.main device pair --json --label 'PowerShell 5.1'; "
              "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }; "
              "$j = ($raw | Out-String) | ConvertFrom-Json; "
              "Write-Output ('PSVERSION=' + $PSVersionTable.PSVersion.ToString()); "
              "Write-Output ('PSEDITION=' + $PSVersionTable.PSEdition); "
              "Write-Output ('CODE=' + $j.pairing_code); Write-Output ('EXPIRES=' + $j.expires_at)")
    result = ctx.run.command("device_pair_powershell51", [str(POWERSHELL), "-NoProfile", "-NonInteractive",
                                                          "-Command", script], ctx.env, check=False)
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    code = fields.get("CODE", "").strip()
    ctx.remember("code", code)
    version = fields.get("PSVERSION", "")
    ctx.run.record("pairing_code_powershell51", result.returncode == 0 and version.startswith("5.1")
                   and fields.get("PSEDITION", "").strip() == "Desktop" and bool(CODE_PATTERN.fullmatch(code))
                   and bool(fields.get("EXPIRES")), exit=result.returncode, ps_version=version.strip(),
                   ps_edition=fields.get("PSEDITION", "").strip())
    return code or None


def session_only_writes(ctx: Context, browser: Browser) -> tuple[str, dict[str, Any]]:
    """The delivery criterion: project-scoped writes with ONLY the session cookie, CSRF and Origin."""
    run, api = ctx.run, SessionApi(browser)
    project = dogfood.create_project(run, api, ctx.env, ctx.root, "identity-synthetic", broken=False)  # type: ignore[arg-type]
    run.record("project_create_and_list_via_session", bool(project), project_id=project,
               creation="pados project create (CLI, direct database: no HTTP project-creation route exists for "
                        "the control token or a session)", listed_with="GET /control/projects (session only)")
    created = browser.send("POST", "/control/work-requests", {
        "project_id": project, "title": "Session-only work request", "idempotency_key": "identity-e2e-wr",
        "body": "Synthetic: created with the session cookie, X-PADOS-CSRF and Origin only."})
    request_id = ((created.body or {}).get("work_request") or {}).get("id") if isinstance(created.body, dict) else None
    read = browser.send("GET", f"/control/work-requests/{request_id}")
    sent = browser.headers("POST")
    run.record("work_request_via_session_only", created.status == 200 and bool(request_id) and read.status == 200
               and "X-PADOS-Control-Token" not in sent, http=created.status, work_request_id=request_id,
               read_back=read.status, request_headers=sorted(sent))
    flow = dogfood.task_flow(api, project, f"identity-{int(time.time())}")  # type: ignore[arg-type]
    (run.out / "session_task_flow.json").write_text(json.dumps(flow, indent=2, default=str), encoding="utf-8")
    submitted = browser.send("POST", f"/control/loops/{flow['loop_id']}/result-candidate", {})
    candidate = (submitted.body or {}).get("candidate", {}) if isinstance(submitted.body, dict) else {}
    run.record("task_loop_and_result_candidate_via_session_only", flow["approve_http"] == 200
               and flow["approval_status"] == "APPROVED" and flow["final_status"] == "COMPLETED"
               and submitted.status == 200 and candidate.get("verification_status") == "PASSED"
               and candidate.get("decision_status") == "WAITING_APPROVAL", final=flow["final_status"],
               work_route_approval=flow["approval_status"], candidate_http=submitted.status,
               verification=candidate.get("verification_status"), decision=candidate.get("decision_status"))
    return project, candidate


def refusals(ctx: Context, browser: Browser, project: str) -> None:
    run = ctx.run
    body = {"project_id": project, "title": "Must be refused", "body": "Synthetic refusal probe.",
            "idempotency_key": "identity-e2e-refused"}
    path = "/control/work-requests"
    cases = {
        "missing_csrf": (browser.send("POST", path, body, csrf=None), 403, "CSRF_INVALID"),
        "wrong_csrf": (browser.send("POST", path, body, csrf="forged-" + secrets.token_urlsafe(16)), 403,
                       "CSRF_INVALID"),
        "missing_origin": (browser.send("POST", path, body, origin=None), 403, "ORIGIN_NOT_ALLOWED"),
        "wrong_origin": (browser.send("POST", path, body, origin=CROSS_ORIGIN), 403, "ORIGIN_NOT_ALLOWED"),
        "sec_fetch_site_cross_site_post": (browser.send("POST", path, body, fetch_site="cross-site"), 403,
                                           "ORIGIN_NOT_ALLOWED"),
        "sec_fetch_site_cross_site_get": (browser.send("GET", "/control/projects", fetch_site="cross-site"), 403,
                                          "ORIGIN_NOT_ALLOWED"),
        "session_get_cross_site": (browser.send("GET", "/identity/v1/session", fetch_site="cross-site"), 403,
                                   "ORIGIN_NOT_ALLOWED"),
        "forged_cookie": (browser.send("POST", path, body, cookie=secrets.token_urlsafe(32)), 403,
                          "SESSION_REQUIRED"),
        "invalid_token_header_with_valid_cookie": (browser.send("POST", path, body, extra={
            "X-PADOS-Control-Token": "not-the-" + secrets.token_urlsafe(16)}), 403, None),
        "no_credential": (ctx.http.request("POST", path, body, {"Origin": browser.origin}), 403, None),
    }
    detail = {name: {"http": reply.status, "code": reply.code, "expected": [status, code]}
              for name, (reply, status, code) in cases.items()}
    ok = all(reply.status == status and (code is None or reply.code == code) for reply, status, code in cases.values())
    ok = ok and all(case[0].code not in (None, "") for case in cases.values())
    listing = browser.send("GET", "/control/projects")
    run.record("refusals_with_closed_codes", ok and listing.status == 200, cases=detail,
               session_still_valid_after=listing.status)
    minted = browser.send("POST", "/identity/v1/pairing-codes", {})
    run.record("session_cannot_mint_pairing_codes", minted.status == 403 and "pairing_code" not in minted.text,
               http=minted.status, code=minted.code)


def step_up_not_bypassed(ctx: Context, browser: Browser, candidate: dict[str, Any]) -> None:
    run, ref = ctx.run, candidate.get("approval_ref")
    before = browser.send("GET", f"/control/approvals/{ref}")
    approval = (before.body or {}).get("approval", {}) if isinstance(before.body, dict) else {}
    decide = {"version": approval.get("version"), "idempotency_key": "identity-e2e-bypass"}
    by_session = browser.send("POST", f"/control/approvals/{ref}/approve", decide)
    _, by_token = ctx.token_api.call("POST", f"/control/approvals/{ref}/approve",
                                     {**decide, "idempotency_key": "identity-e2e-bypass-token"})
    token_reply = ctx.http.replies[-1]
    after = browser.send("GET", f"/control/approvals/{ref}")
    status_after = ((after.body or {}).get("approval") or {}).get("status") if isinstance(after.body, dict) else None
    run.record("step_up_cannot_be_bypassed_by_session", bool(ref) and approval.get("status") == "PENDING"
               and by_session.status == 403 and by_session.code == STEP_UP_CODE and token_reply.status == 403
               and token_reply.code == STEP_UP_CODE and status_after == "PENDING", approval_ref=ref,
               session_http=by_session.status, session_code=by_session.code, token_http=token_reply.status,
               token_code=token_reply.code, status_before=approval.get("status"), status_after=status_after)
    del by_token


def restart_keeps_session(ctx: Context, server: subprocess.Popen[str], browser: Browser) -> subprocess.Popen[str]:
    dogfood.stop_server(server)
    server = dogfood.start_server(ctx.run, ctx.env, ctx.api_port, "serve_identity_restarted")
    reply = browser.send("GET", "/identity/v1/session")
    body = reply.body if isinstance(reply.body, dict) else {}
    write = browser.send("GET", "/control/projects")
    ctx.run.record("session_survives_server_restart", body.get("authenticated") is True
                   and body.get("device_id") == browser.device_id and body.get("csrf_token") == browser.csrf
                   and write.status == 200, http=reply.status, control_read=write.status)
    return server


def revoke_and_logout(ctx: Context, browser: Browser) -> None:
    run = ctx.run
    victim = ctx.browser("E2E browser B")
    ctx.pair(victim, ctx.mint_code("device_pair_b", "E2E browser B"))
    alive = victim.send("GET", "/control/projects").status
    revoked = browser.send("POST", f"/identity/v1/devices/{victim.device_id}/revoke", {})
    session = victim.send("GET", "/identity/v1/session")
    write = victim.send("POST", "/control/work-requests", {"project_id": "x", "title": "t", "body": "b",
                                                           "idempotency_key": "identity-e2e-revoked"})
    run.record("device_revoke_kills_other_devices_session", alive == 200 and revoked.status == 200
               and (session.body or {}).get("authenticated") is False and write.status == 403
               and write.code == "SESSION_REVOKED" and browser.send("GET", "/control/projects").status == 200,
               before=alive, revoke_http=revoked.status, victim_session=(session.body or {}).get("authenticated"),
               victim_write=write.status, victim_code=write.code)

    third = ctx.browser("E2E browser C")
    ctx.pair(third, ctx.mint_code("device_pair_c", "E2E browser C"))
    listed = ctx.cli("device_list_cli_json", "device", "list", "--json")
    devices, _ = json.JSONDecoder().raw_decode(listed.stdout.lstrip())
    leaked = [v for kind in ("session", "csrf") for v in ctx.secrets_seen[kind] if v in listed.stdout]
    revoke_cli = ctx.cli("device_revoke_cli", "device", "revoke", third.device_id)
    dead = third.send("GET", "/control/projects")
    run.record("cli_device_list_and_revoke", listed.returncode == 0 and not leaked
               and any(d.get("device_id") == third.device_id for d in devices.get("devices", []))
               and revoke_cli.returncode == 0 and dead.status == 403 and dead.code == "SESSION_REVOKED",
               devices=len(devices.get("devices", [])), secrets_in_listing=len(leaked),
               revoke_exit=revoke_cli.returncode, revoked_session_http=dead.status, revoked_session_code=dead.code)

    out = browser.send("POST", "/identity/v1/logout", {})
    cleared = any("pados_session=" in line and ("max-age=0" in line.lower() or "expires=" in line.lower())
                  for line in out.header("set-cookie"))
    after = browser.send("GET", "/control/projects")
    session = browser.send("GET", "/identity/v1/session")
    run.record("logout_revokes_current_session", out.status == 200 and cleared and after.status == 403
               and after.code == "SESSION_REVOKED" and (session.body or {}).get("authenticated") is False,
               http=out.status, cookie_cleared=cleared, after_http=after.status, after_code=after.code)


def idle_expiry(ctx: Context, wait_seconds: int, skip_reason: str | None) -> None:
    if skip_reason:
        note(ctx.run, "idle_expiry", "SKIPPED", reason=skip_reason)
        return
    idle = ctx.browser("E2E idle browser")
    ctx.pair(idle, ctx.mint_code("device_pair_idle", "E2E idle browser"))
    first = idle.send("GET", "/control/projects").status
    print(f"waiting {wait_seconds} s for the {IDLE_TIMEOUT_SECONDS} s idle timeout", flush=True)
    time.sleep(wait_seconds)
    expired = idle.send("GET", "/control/projects")
    session = idle.send("GET", "/identity/v1/session")
    ctx.run.record("idle_expiry", first == 200 and expired.status == 403 and expired.code == "SESSION_EXPIRED"
                   and (session.body or {}).get("authenticated") is False, before=first, waited_seconds=wait_seconds,
                   idle_timeout_seconds=IDLE_TIMEOUT_SECONDS, after_http=expired.status, after_code=expired.code)


def oidc_configs(ctx: Context, project: str) -> dict[str, str]:
    """A generic provider whose HTTPS issuer is a closed loopback port, and one actor bound to it."""
    issuer = f"https://127.0.0.1:{free_port()}"
    provider = {"id": "e2e", "kind": "generic", "display_name": "E2E unreachable issuer", "issuer": issuer,
                "client_id": "pados-identity-e2e", "client_secret_env": "PADOS_OIDC_E2E_UNSET",
                "redirect_uri": f"http://127.0.0.1:{ctx.api_port}/identity/v1/oidc/e2e/callback"}
    oidc = ctx.root / "identity-oidc.json"
    oidc.write_text(json.dumps({"schema": "pados.identity.oidc.v1", "post_login_redirect": "/",
                                "providers": [provider]}), encoding="utf-8")
    actors = ctx.root / "identity-actors.json"
    actors.write_text(json.dumps({"schema": "pados.identity.actors.v1", "actors": [{
        "actor_id": "e2e-actor", "issuer": issuer, "subject": "e2e-subject",
        "project_roles": {project: ["contributor"]}}]}), encoding="utf-8")
    return {"PADOS_IDENTITY_OIDC_CONFIG": str(oidc), "PADOS_IDENTITY_ACTORS_CONFIG": str(actors)}


def rotation(ctx: Context, server: subprocess.Popen[str], project: str) -> subprocess.Popen[str]:
    """``pados init --force`` rotates the control token, which ends every session bound to the old hash."""
    run = ctx.run
    survivor = ctx.browser("E2E rotation browser")
    ctx.pair(survivor, ctx.mint_code("device_pair_rotation", "E2E rotation browser"))
    before = survivor.send("GET", "/control/projects").status
    old_value = ctx.control_value
    dogfood.stop_server(server)
    ctx.cli("pados_init_force_rotation", "init", "--dir", str(ctx.root), "--force", check=True)
    ctx.control_value = (ctx.root / "runtime" / "local" / "control.token").read_text(encoding="utf-8").strip()
    ctx.remember("control", ctx.control_value)
    ctx.env["PADOS_CONTROL_API_TOKEN_HASH"] = hashlib.sha256(ctx.control_value.encode()).hexdigest().upper()
    ctx.env.update(oidc_configs(ctx, project))
    server = dogfood.start_server(run, ctx.env, ctx.api_port, "serve_after_rotation")
    after = survivor.send("GET", "/control/projects")
    session = survivor.send("GET", "/identity/v1/session")
    old, _ = ctx.token_api.call("GET", "/control/projects", control=old_value)
    new, _ = ctx.token_api.call("GET", "/control/projects")
    fresh = ctx.browser("E2E browser after rotation")
    repaired = ctx.pair(fresh, ctx.mint_code("device_pair_after_rotation", "E2E browser after rotation"))
    run.record("control_token_rotation_ends_sessions", before == 200 and old_value != ctx.control_value
               and after.status == 403 and after.code == "SESSION_REVOKED"
               and (session.body or {}).get("authenticated") is False and old == 403 and new == 200
               and repaired.status == 200 and fresh.send("GET", "/control/projects").status == 200,
               before=before, after_http=after.status, after_code=after.code, old_token=old, new_token=new,
               pair_after_rotation=repaired.status, rotation="pados init --dir <root> --force, then restart")
    return server


def oidc_fail_closed(ctx: Context) -> None:
    run = ctx.run
    session = ctx.http.request("GET", "/identity/v1/session")
    providers = (session.body or {}).get("providers") if isinstance(session.body, dict) else None
    start = ctx.http.request("GET", "/identity/v1/oidc/e2e/start", headers={"Sec-Fetch-Site": "none"})
    unknown = ctx.http.request("GET", "/identity/v1/oidc/nope/start", headers={"Sec-Fetch-Site": "none"})
    callback = ctx.http.request("GET", "/identity/v1/oidc/e2e/callback?code=forged&state=forged")
    location = ",".join(callback.header("location"))
    actor_cookie = any(line.startswith(("pados_actor=", "__Host-pados_actor=")) and "max-age=0" not in line.lower()
                       for line in callback.header("set-cookie") + start.header("set-cookie"))
    me = ctx.http.request("GET", "/identity/v1/me")
    run.record("oidc_configured_provider_fails_closed", isinstance(providers, list) and len(providers) == 1
               and providers[0].get("id") == "e2e" and start.status == 503 and start.code == "OIDC_PROVIDER_UNAVAILABLE"
               and unknown.status == 404 and unknown.code == "OIDC_PROVIDER_NOT_CONFIGURED"
               and callback.status == 302 and "sso_error=OIDC_STATE_INVALID" in location and not actor_cookie
               and me.status == 403, providers=providers, start=[start.status, start.code],
               unknown=[unknown.status, unknown.code], callback=[callback.status, location], me=[me.status, me.code])
    note(run, "oidc_live_sign_in", "NOT_LIVE_VALIDATED",
         reason="OIDC discovery, JWKS and token exchange are HTTPS-only against the system trust store "
                "(httpx trust_env=False), so a local fake issuer cannot be reached by a real pados serve without "
                "weakening that rule; the sign-in flow is covered by tests/identity/oidc (fake issuer, mocked "
                "transport). Microsoft Entra ID and Google remain NOT_LIVE_VALIDATED.")


def playwright_live(ctx: Context, web_port: int, project_name: str) -> None:
    run = ctx.run
    if port_busy(web_port):
        run.record("playwright_identity_live", False, reason=f"web port {web_port} is already in use")
        return
    web = REPO / "web"
    env = {k: v for k, v in os.environ.items() if not k.startswith("PADOS_")}
    env.update({"PADOS_WEB_PORT": str(web_port), "PADOS_WEB_BACKEND_URL": f"http://127.0.0.1:{ctx.api_port}",
                "PADOS_E2E_IDENTITY_LIVE": "1", "PADOS_E2E_PYTHON": sys.executable,
                "PADOS_E2E_API_URL": f"http://127.0.0.1:{ctx.api_port}",
                "PADOS_E2E_TOKEN_FILE": str(ctx.root / "runtime" / "local" / "control.token"),
                "PADOS_E2E_PROJECT_NAME": project_name, "PYTHONUTF8": "1"})
    argv = ["npx", "playwright", "test", "e2e/identity-live.spec.ts", "--reporter=list", "--workers=1",
            "--output", str(run.out / "playwright-results")]
    log = run.out / "playwright_identity_live.log"
    result = subprocess.run(argv, cwd=web, env=env, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=900, shell=os.name == "nt")
    log.write_text(f"$ {' '.join(argv)}\nexit={result.returncode}\n--- stdout\n{result.stdout}\n--- stderr\n"
                   f"{result.stderr}", encoding="utf-8")
    summary = [line.strip() for line in result.stdout.splitlines() if re.search(r"\d+ (passed|failed|skipped)", line)]
    run.record("playwright_identity_live", result.returncode == 0 and any("passed" in s for s in summary)
               and not any("failed" in s or "skipped" in s for s in summary), exit=result.returncode,
               summary=summary, web_port=web_port)


def leak_scan(ctx: Context) -> None:
    """No control token anywhere the run captured; no session id, CSRF value or pairing code in a server log."""
    tokens = ctx.secrets_seen["control"]
    bodies = [r.text + "\n" + "\n".join(f"{k}: {v}" for k, v in r.headers) for r in ctx.http.replies]
    in_replies = sum(1 for text in bodies for token in tokens if token in text)
    files = [p for p in ctx.run.out.rglob("*") if p.is_file() and p.suffix in {".log", ".json", ".txt", ".md"}]
    in_files = sorted(p.name for p in files for token in tokens if token in p.read_text("utf-8", "replace"))
    server_logs = sorted(ctx.run.out.glob("serve_*.log"))
    log_text = "\n".join(p.read_text("utf-8", "replace") for p in server_logs)
    codes = {c for code in ctx.secrets_seen["code"] for c in (code, code.replace("-", ""))}
    in_server_logs = {kind: sum(1 for v in values if v and v in log_text) for kind, values in
                      (("session", ctx.secrets_seen["session"]), ("csrf", ctx.secrets_seen["csrf"]),
                       ("pairing_code", codes), ("control", tokens))}
    ctx.run.record("no_control_token_or_session_secret_leak", bool(tokens) and in_replies == 0 and not in_files
                   and not any(in_server_logs.values()) and bool(server_logs), replies_scanned=len(bodies),
                   files_scanned=len(files), server_logs=[p.name for p in server_logs],
                   control_token_in_replies=in_replies, control_token_in_files=in_files,
                   secrets_in_server_logs=in_server_logs, control_tokens_checked=len(tokens),
                   sessions_checked=len(ctx.secrets_seen["session"]), codes_checked=len(ctx.secrets_seen["code"]))


# --- orchestration ----------------------------------------------------------------------------------------------------
def prepare(args: argparse.Namespace, run: dogfood.Run, root: Path, db_pass: str) -> dict[str, str]:
    env = dogfood.base_env(args, root, db_pass)
    init = run.command("pados_init", [sys.executable, "-m", "pados.cli.main", "init", "--dir", str(root)], env)
    control_value = (root / "runtime" / "local" / "control.token").read_text(encoding="utf-8").strip()
    lines = (root / ".env").read_text(encoding="utf-8").splitlines()
    token_hash = next(line.split("=", 1)[1] for line in lines if line.startswith("PADOS_CONTROL_API_TOKEN_HASH="))
    store = next(line.split("=", 1)[1] for line in lines if line.startswith("PADOS_IDENTITY_STORE_DIR="))
    web = [f"http://127.0.0.1:{args.web_port}", f"http://localhost:{args.web_port}"]
    env.update({"PADOS_CONTROL_API_TOKEN_HASH": token_hash, "PADOS_IDENTITY_SESSIONS_ENABLED": "true",
                "PADOS_IDENTITY_STORE_DIR": store, "PADOS_IDENTITY_IDLE_TIMEOUT_SECONDS": str(IDLE_TIMEOUT_SECONDS),
                "PADOS_IDENTITY_ALLOWED_ORIGINS": ",".join([*web, f"http://127.0.0.1:{args.api_port}"])})
    run.record("pados_init_unique_token", hashlib.sha256(control_value.encode()).hexdigest().upper() == token_hash
               and control_value != dogfood.DEFAULT_TOKEN_PLAINTEXT and Path(store).is_absolute()
               and "PADOS_IDENTITY_SESSIONS_ENABLED=false" in lines, init_exit=init.returncode,
               identity_store_under_root=Path(store).is_relative_to(root))
    dogfood.create_databases(args.pg_host, args.pg_port, args.pg_user, db_pass, list(dogfood.LANES.values()))
    for lane, ini, revision in dogfood.MIGRATIONS:
        run.command(f"migrate_{lane}_{revision}", [sys.executable, "-m", "alembic", "-c", ini, "upgrade", revision],
                    {**env, "PADOS_DB_NAME": dogfood.LANES[lane]})
    run.record("migrations", True, lanes=dogfood.LANES, revisions=[m[2] for m in dogfood.MIGRATIONS])
    return env


def exercise(args: argparse.Namespace, run: dogfood.Run, root: Path, env: dict[str, str]) -> None:
    control_value = (root / "runtime" / "local" / "control.token").read_text(encoding="utf-8").strip()
    ctx = Context(run, env, root, Http(args.api_port), args.api_port, f"http://127.0.0.1:{args.web_port}",
                  control_value)
    ctx.remember("control", control_value)
    server = dogfood.start_server(run, env, args.api_port, "serve_identity")
    try:
        browser = pairing(ctx)
        project, candidate = session_only_writes(ctx, browser)
        refusals(ctx, browser, project)
        step_up_not_bypassed(ctx, browser, candidate)
        server = restart_keeps_session(ctx, server, browser)
        revoke_and_logout(ctx, browser)
        if args.playwright:
            playwright_live(ctx, args.web_port, "identity-synthetic")
        else:
            note(run, "playwright_identity_live", "SKIPPED", reason="run without --playwright")
        idle_expiry(ctx, args.idle_wait, args.skip_idle)
        server = rotation(ctx, server, project)
        oidc_fail_closed(ctx)
    finally:
        dogfood.stop_server(server)
    leak_scan(ctx)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--pg-bin", type=Path, help="native PostgreSQL bin dir: start a private loopback cluster")
    parser.add_argument("--pg-host", default="127.0.0.1")
    parser.add_argument("--pg-port", type=int, default=55450)
    parser.add_argument("--pg-user", default="pados")
    parser.add_argument("--api-port", type=int, default=18091)
    parser.add_argument("--web-port", type=int, default=5181)
    parser.add_argument("--playwright", action="store_true", help="also run web/e2e/identity-live.spec.ts")
    parser.add_argument("--idle-wait", type=int, default=IDLE_TIMEOUT_SECONDS + 10)
    parser.add_argument("--skip-idle", metavar="REASON", help="skip the idle-expiry wait, recording this reason")
    args = parser.parse_args(argv)
    if args.idle_wait <= IDLE_TIMEOUT_SECONDS:
        parser.error(f"--idle-wait must exceed the {IDLE_TIMEOUT_SECONDS} s idle timeout")
    run = dogfood.Run(args.out.resolve())
    root = Path(tempfile.mkdtemp(prefix="pados-identity-e2e-"))
    db_pass = "" if args.pg_bin else os.environ.get("PADOS_DB_PASSWORD", "")
    cluster = dogfood.Cluster(args.pg_bin, root, args.pg_port) if args.pg_bin else None
    started = time.time()
    try:
        if cluster:
            cluster.start()
        run.record("postgres_ready", True, mode="private-cluster" if cluster else "existing-server", port=args.pg_port)
        env = prepare(args, run, root, db_pass)
        exercise(args, run, root, env)
    except Exception as exc:  # noqa: BLE001 - every failure is recorded as evidence, then the run fails
        run.record("unexpected_failure", False, error=f"{type(exc).__name__}: {exc}"[:800])
    finally:
        if cluster:
            cluster.stop()
        with contextlib.suppress(OSError):
            shutil.rmtree(root, ignore_errors=True)
    passed = not any(step["status"] == "FAIL" for step in run.steps)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True).stdout.strip()
    counts = {status: sum(1 for s in run.steps if s["status"] == status) for status in
              ("PASS", "FAIL", "SKIPPED", "NOT_LIVE_VALIDATED")}
    (run.out / "IDENTITY_E2E_RESULT.json").write_text(json.dumps({
        "runner_contract": CONTRACT, "verdict": "PASS" if passed else "FAIL", "commit": head,
        "platform": platform.platform(), "python": sys.version.split()[0], "os_name": os.name,
        "seconds": int(time.time() - started), "finished": datetime.now(UTC).isoformat(), "counts": counts,
        "options": {"playwright": args.playwright, "idle_wait": None if args.skip_idle else args.idle_wait,
                    "pg_mode": "private-cluster" if cluster else "existing-server"},
        "steps": run.steps}, indent=2, default=str), encoding="utf-8")
    print(f"IDENTITY E2E {'PASS' if passed else 'FAIL'} {counts} -> {run.out / 'IDENTITY_E2E_RESULT.json'}",
          flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
