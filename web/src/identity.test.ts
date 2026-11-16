// SPDX-License-Identifier: AGPL-3.0-only
// Unified Identity & Session, phase 1 (docs/identity/UNIFIED_IDENTITY_SESSION_DESIGN.md §3): the page restores a
// paired session on reload, pairs with a one-time code, and then makes changes without any control token (the
// cookie, plus X-PADOS-CSRF on writes only). With pairing off or not done, the control token path is unchanged.
// Nothing is written to browser storage, and a refused session write is reported, never retried.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CONTROL_TOKEN_HEADER, controlFailure, controlRequest, createWorkRequest, decideApproval, decisionPrompt,
  getApproval, WorkRequestPage, type ControlApproval } from "./control-actions";
import { CSRF_HEADER, SAME_ORIGIN_COOKIES, authorizedInit, hasControlAuth, type SessionAuth } from "./control-auth";
import { HttpError } from "./http";
import {
  IDENTITY_MESSAGES,
  confirmAndRevoke,
  controlAuthFor,
  defaultDeviceLabel,
  getSession,
  identityFailure,
  identityStatusText,
  isCompletePairingCode,
  isSessionLost,
  listDevices,
  loadIdentityView,
  logout,
  normalizePairingCode,
  pair,
  pairFailure,
  revokeDevice,
  revokePrompt,
  sanitizeDeviceLabel,
  sessionEndedNotice,
  type ActiveSession,
  type Device,
  type IdentityView,
} from "./identity";
import { ControlAccessPanel, DeviceList, IdentityPage, type Identity } from "./identity-panel";

type Sent = { url: string; init: RequestInit };
type Reply = { status: number; body: unknown };

/** A fetch that records every request and answers from a queue (the last answer repeats). */
function recorder(...replies: Reply[]): { sent: Sent[]; fetcher: typeof fetch } {
  const sent: Sent[] = [];
  const fetcher = (async (url: string, init: RequestInit) => {
    sent.push({ url, init });
    const reply = replies.length > 1 ? replies.shift()! : replies[0];
    return new Response(JSON.stringify(reply.body), { status: reply.status, headers: { "Content-Type": "application/json" } });
  }) as unknown as typeof fetch;
  return { sent, fetcher };
}

const ok = (body: unknown): Reply => ({ status: 200, body });
const refusal = (status: number, code: string, writeOccurred = "NO"): Reply => ({ status, body: {
  error: { schema_version: "pados.error.v1", code, category: "AUTH", message: code, retryable: false, request_id: "srv-1",
    write_occurred: writeOccurred } } });

function headers(init: RequestInit): Record<string, string> {
  return init.headers as Record<string, string>;
}

const CSRF = "csrf-derived-value-123";
const pairedBody = {
  enabled: true, authenticated: true, device_id: "dev_0123456789abcdef", device_label: "Edge on desk",
  auth_method: "device_pairing", expires_at: "2026-11-17T10:00:00Z", idle_expires_at: "2026-11-16T12:00:00Z",
  csrf_token: CSRF, providers: [],
};
const session: ActiveSession = {
  deviceId: "dev_0123456789abcdef", deviceLabel: "Edge on desk", authMethod: "device_pairing",
  expiresAt: "2026-11-17T10:00:00Z", csrfToken: CSRF,
};
const sessionAuth: SessionAuth = { kind: "session", csrfToken: CSRF, deviceId: session.deviceId };
const approval: ControlApproval = { id: "a1", project_id: "p1", status: "PENDING", version: 2, subject_kind: null, subject_json: null };

beforeEach(() => { vi.stubGlobal("location", { hostname: "127.0.0.1" }); });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("restoring the session on reload", () => {
  it("restores a paired session from the cookie, with a same-origin GET that carries no CSRF and no token", async () => {
    const { sent, fetcher } = recorder(ok(pairedBody));
    const view = await loadIdentityView(fetcher);
    expect(view).toMatchObject({ phase: "paired", session: { deviceId: "dev_0123456789abcdef", deviceLabel: "Edge on desk",
      csrfToken: CSRF, idleExpiresAt: "2026-11-16T12:00:00Z" } });
    expect(sent).toHaveLength(1);
    expect(sent[0].url).toBe("/identity/v1/session");
    expect(sent[0].init).toMatchObject({ method: "GET", ...SAME_ORIGIN_COOKIES });
    expect(headers(sent[0].init)[CSRF_HEADER]).toBeUndefined();
    expect(headers(sent[0].init)[CONTROL_TOKEN_HEADER]).toBeUndefined();
  });

  it("reports not paired, pairing off, and an older server distinctly", async () => {
    expect(await loadIdentityView(recorder(ok({ enabled: true, authenticated: false, providers: [] })).fetcher))
      .toEqual({ phase: "unpaired", providers: [] });
    expect(await loadIdentityView(recorder(ok({ enabled: false, authenticated: false, providers: [] })).fetcher))
      .toEqual({ phase: "disabled" });
    const old = await loadIdentityView(recorder({ status: 404, body: { detail: "Not Found" } }).fetcher);
    expect(old).toMatchObject({ phase: "unavailable", reason: expect.stringContaining("does not offer browser pairing") });
  });

  it("ignores any session the server reports while pairing is off", async () => {
    expect(await loadIdentityView(recorder(ok({ ...pairedBody, enabled: false })).fetcher)).toEqual({ phase: "disabled" });
  });

  it("falls back to the token path when the answer is unusable or the server cannot be reached", async () => {
    const noCsrf = await loadIdentityView(recorder(ok({ ...pairedBody, csrf_token: "" })).fetcher);
    expect(noCsrf.phase).toBe("unavailable");
    const down = (async () => { throw new TypeError("Failed to fetch"); }) as unknown as typeof fetch;
    expect((await loadIdentityView(down)).phase).toBe("unavailable");
  });

  it("asks nothing from a page served by another host", async () => {
    vi.stubGlobal("location", { hostname: "jimmy0307.github.io" });
    const { sent, fetcher } = recorder(ok(pairedBody));
    expect((await loadIdentityView(fetcher)).phase).toBe("unavailable");
    await expect(pair("ABCDE-FGHJK", "x", fetcher)).rejects.toThrow(/IDENTITY_LOCAL_ONLY/);
    expect(sent).toHaveLength(0);
  });

  it("keeps sign-in providers only when the server lists some", async () => {
    expect((await getSession(recorder(ok({ enabled: true, authenticated: false })).fetcher)).providers).toEqual([]);
    const listed = await getSession(recorder(ok({ enabled: true, authenticated: false,
      providers: [{ id: "entra", display_name: "Microsoft Entra ID" }, "google", 7] })).fetcher);
    expect(listed.providers).toEqual([{ id: "entra", label: "Microsoft Entra ID" }, { id: "google", label: "google" }]);
  });
});

describe("pairing this browser", () => {
  it("sends the grouped code and the label, without CSRF or token, and keeps the returned CSRF token", async () => {
    const { sent, fetcher } = recorder(ok({ device_id: "dev_1111222233334444", device_label: "Laptop", csrf_token: CSRF,
      expires_at: "2026-11-17T10:00:00Z", idle_timeout_seconds: 7200 }));
    const paired = await pair(" abcde fghjk ", "  Laptop\u0007 ", fetcher, Date.parse("2026-11-16T08:00:00Z"));
    expect(sent[0].url).toBe("/identity/v1/pair");
    expect(sent[0].init).toMatchObject({ method: "POST", ...SAME_ORIGIN_COOKIES });
    expect(JSON.parse(String(sent[0].init.body))).toEqual({ pairing_code: "ABCDE-FGHJK", device_label: "Laptop" });
    expect(headers(sent[0].init)[CSRF_HEADER]).toBeUndefined();
    expect(headers(sent[0].init)[CONTROL_TOKEN_HEADER]).toBeUndefined();
    expect(paired).toEqual({ deviceId: "dev_1111222233334444", deviceLabel: "Laptop", authMethod: "device_pairing",
      expiresAt: "2026-11-17T10:00:00Z", idleExpiresAt: "2026-11-16T10:00:00.000Z", csrfToken: CSRF });
  });

  it("names the browser when the person gave no label", async () => {
    const { sent, fetcher } = recorder(ok({ device_id: "dev_1", csrf_token: CSRF }));
    vi.stubGlobal("navigator", { userAgent: "Mozilla/5.0 Chrome/130.0 Safari/537.36 Edg/130.0" });
    await pair("ABCDE-FGHJK", "", fetcher);
    expect(JSON.parse(String(sent[0].init.body)).device_label).toBe("Edge browser");
  });

  it("sends nothing for an incomplete code", async () => {
    const { sent, fetcher } = recorder(ok({}));
    const failure = await pair("ABCDE-FG", "", fetcher).catch((error: unknown) => error);
    expect(identityFailure(failure)).toContain("whole 10-character code");
    expect(sent).toHaveLength(0);
  });

  it.each([
    ["PAIRING_CODE_INVALID", 400, "was not accepted"],
    ["PAIRING_CODE_EXPIRED", 400, "has expired"],
    ["PAIRING_RATE_LIMITED", 429, "Pairing is paused"],
    ["IDENTITY_DISABLED", 403, "PADOS_IDENTITY_SESSIONS_ENABLED=true and restart pados serve"],
    ["ORIGIN_NOT_ALLOWED", 403, "PADOS_IDENTITY_ALLOWED_ORIGINS"],
    ["DEVICE_LIMIT", 409, "device limit"],
  ])("explains a refused pairing (%s)", async (code, status, words) => {
    const { sent, fetcher } = recorder(refusal(status, code));
    const failure = await pair("ABCDE-FGHJK", "", fetcher).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(HttpError);
    const message = identityFailure(failure);
    expect(message).toContain(words);
    expect(message).toContain(code);
    expect(message).toContain("request srv-1");
    expect(sent).toHaveLength(1);  // a refused pairing is not retried
  });

  it("asks for a new code when the pairing answer never arrived (the code is single-use, it is not resent)", async () => {
    let calls = 0;
    const dropped = (async () => { calls += 1; throw new TypeError("Failed to fetch"); }) as unknown as typeof fetch;
    const failure = await pair("ABCDE-FGHJK", "", dropped).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(HttpError);
    expect((failure as HttpError).outcome).toBe("WRITE_OUTCOME_UNKNOWN");
    expect(calls).toBe(1);
    const message = pairFailure(failure);
    expect(message).toContain("did not reach this page");
    expect(message).toContain("pados device pair");
    expect(message).toContain(`request ${(failure as HttpError).requestId}`);
    // Any other refusal keeps its own explanation.
    const refused = new HttpError("SERVER_ERROR", 400, "PAIRING_CODE_EXPIRED", "r-1", false, "NO");
    expect(pairFailure(refused)).toBe(identityFailure(refused));
  });

  it("does not reveal whether an unknown or a wrong code was sent", () => {
    expect(IDENTITY_MESSAGES.PAIRING_CODE_INVALID).not.toMatch(/unknown|wrong|exist/i);
  });
});

describe("pairing code input", () => {
  it.each([
    ["abcde", "ABCDE"],
    ["abcdef", "ABCDE-F"],
    ["abcde fghjk", "ABCDE-FGHJK"],
    ["  ab cd ef gh jk  ", "ABCDE-FGHJK"],
    ["ABCDE-FGHJK", "ABCDE-FGHJK"],
    ["abcde--fghjk", "ABCDE-FGHJK"],
    ["ABCDEFGHJKMN", "ABCDE-FGHJK"],
    ["a.b_c/d e", "ABCDE"],
    ["", ""],
  ])("normalises %j to %j", (raw, expected) => {
    expect(normalizePairingCode(raw)).toBe(expected);
  });

  it("knows a complete code", () => {
    expect(isCompletePairingCode("abcde fghjk")).toBe(true);
    expect(isCompletePairingCode("ABCDE-FGHJ")).toBe(false);
  });

  it("keeps labels printable and short", () => {
    expect(sanitizeDeviceLabel("  a\u0000b\nc  ")).toBe("a b c");
    expect(sanitizeDeviceLabel("x".repeat(100))).toHaveLength(64);
    expect(defaultDeviceLabel("Mozilla/5.0 Firefox/130.0")).toBe("Firefox browser");
    expect(defaultDeviceLabel("")).toBe("Browser");
  });
});

describe("control requests with a paired session", () => {
  it("send no control token and add X-PADOS-CSRF only to state-changing methods", async () => {
    const { sent, fetcher } = recorder(ok({ status: "ok", approval }));
    await getApproval(sessionAuth, "a1", fetcher);
    await decideApproval(sessionAuth, approval, "approve", "", fetcher);
    await createWorkRequest(sessionAuth, "p1", "t", "b", fetcher, "web-work-k");
    const [read, decide, create] = sent;
    expect(headers(read.init)[CSRF_HEADER]).toBeUndefined();
    expect(headers(decide.init)[CSRF_HEADER]).toBe(CSRF);
    expect(headers(create.init)[CSRF_HEADER]).toBe(CSRF);
    for (const request of sent) {
      expect(request.init).toMatchObject(SAME_ORIGIN_COOKIES);
      expect(headers(request.init)[CONTROL_TOKEN_HEADER]).toBeUndefined();
      expect(request.url).not.toContain(CSRF);  // never in a URL
      expect(String(request.init.body ?? "")).not.toContain(CSRF);  // only in its header
    }
  });

  it.each(["POST", "PUT", "PATCH", "DELETE", "post"])("adds the CSRF header to %s", (method) => {
    expect(headers(authorizedInit(sessionAuth, { method }))[CSRF_HEADER]).toBe(CSRF);
  });

  it.each(["GET", "HEAD", "OPTIONS", undefined])("leaves the CSRF header off %s", (method) => {
    expect(headers(authorizedInit(sessionAuth, { method }))[CSRF_HEADER]).toBeUndefined();
  });

  it.each(["SESSION_EXPIRED", "SESSION_REVOKED", "CSRF_INVALID"])(
    "report a write refused with %s clearly, mark the session for a fresh read, and never retry", async (code) => {
      const { sent, fetcher } = recorder(refusal(code === "CSRF_INVALID" ? 403 : 401, code));
      const failure = await createWorkRequest(sessionAuth, "p1", "t", "b", fetcher).catch((error: unknown) => error);
      expect(sent).toHaveLength(1);
      expect(isSessionLost(failure)).toBe(true);
      expect(controlFailure(failure)).toContain(code);
      expect(controlFailure(failure)).toContain(IDENTITY_MESSAGES[code]);
    });

  it("keep the outcome of an unanswered write unknown, not failed", async () => {
    const down = vi.fn(async () => { throw new TypeError("Failed to fetch"); }) as unknown as typeof fetch;
    const failure = await decideApproval(sessionAuth, approval, "reject", "", down).catch((error: unknown) => error);
    expect(down).toHaveBeenCalledTimes(1);
    expect(controlFailure(failure)).toContain("may or may not have been applied");
  });

  it("keep the step-up guidance when a result acceptance is refused", async () => {
    const { fetcher } = recorder(refusal(403, "RESULT_ACCEPTANCE_REQUIRES_STEP_UP"));
    const failure = await decideApproval(sessionAuth, approval, "approve", "", fetcher).catch((error: unknown) => error);
    expect(controlFailure(failure)).toContain("WebAuthn step-up in the Remote console");
    expect(isSessionLost(failure)).toBe(false);
  });

  it("tell the person what a decision is recorded with", () => {
    expect(decisionPrompt(approval, "approve", sessionAuth)).toContain("paired session (device dev_0123456789abcdef)");
    expect(decisionPrompt(approval, "approve", "tok")).toContain("with the control token");
  });
});

describe("the control token path when there is no session", () => {
  it("is exactly the old request: the token header, no CSRF, no cookie mode change", async () => {
    expect(authorizedInit("tok-1", { method: "POST", body: "{}" })).toEqual({ method: "POST", body: "{}",
      headers: { Accept: "application/json", "Content-Type": "application/json", [CONTROL_TOKEN_HEADER]: "tok-1" } });
    const { sent, fetcher } = recorder(ok({ status: "ok" }));
    await controlRequest("/control/work-requests", "tok-1", { method: "POST", body: "{}" }, fetcher);
    expect(headers(sent[0].init)[CONTROL_TOKEN_HEADER]).toBe("tok-1");
    expect(headers(sent[0].init)[CSRF_HEADER]).toBeUndefined();
    expect(Object.keys(sent[0].init)).not.toContain(Object.keys(SAME_ORIGIN_COOKIES)[0]);
  });

  it.each<[string, IdentityView]>([
    ["disabled", { phase: "disabled" }],
    ["unpaired", { phase: "unpaired", providers: [] }],
    ["unavailable", { phase: "unavailable", reason: "x" }],
    ["loading", { phase: "loading" }],
  ])("uses the typed token while pairing is %s", (_name, view) => {
    expect(controlAuthFor(view, "tok-1")).toBe("tok-1");
  });

  it("uses only the session once paired, whatever token was typed", () => {
    expect(controlAuthFor({ phase: "paired", session, providers: [] }, "tok-1")).toEqual(sessionAuth);
    expect(hasControlAuth(sessionAuth)).toBe(true);
    expect(hasControlAuth("")).toBe(false);
  });
});

describe("logging out and revoking devices", () => {
  const devices: Device[] = [
    { deviceId: "dev_aaaa", deviceLabel: "This laptop", current: true, pairedAt: "2026-11-16T08:00:00Z" },
    { deviceId: "dev_bbbb", deviceLabel: "Old desktop", current: false },
    { deviceId: "dev_cccc", deviceLabel: "Gone", current: false, revokedAt: "2026-11-15T08:00:00Z" },
  ];

  it("logs out the current session with the CSRF token", async () => {
    const { sent, fetcher } = recorder(ok({ status: "ok" }));
    await logout(session, fetcher);
    expect(sent[0].url).toBe("/identity/v1/logout");
    expect(sent[0].init).toMatchObject({ method: "POST", ...SAME_ORIGIN_COOKIES });
    expect(headers(sent[0].init)[CSRF_HEADER]).toBe(CSRF);
    expect(headers(sent[0].init)[CONTROL_TOKEN_HEADER]).toBeUndefined();
  });

  it("lists devices without CSRF, and keeps only well-formed entries", async () => {
    const { sent, fetcher } = recorder(ok({ devices: [
      { device_id: "dev_aaaa", device_label: "This laptop", paired_at: "2026-11-16T08:00:00Z", last_seen_at: null,
        revoked_at: null, current: true },
      { device_label: "no id" },
    ] }));
    expect(await listDevices(sessionAuth, fetcher)).toEqual([{ deviceId: "dev_aaaa", deviceLabel: "This laptop",
      pairedAt: "2026-11-16T08:00:00Z", lastSeenAt: undefined, revokedAt: undefined, current: true }]);
    expect(sent[0].url).toBe("/identity/v1/devices");
    expect(headers(sent[0].init)[CSRF_HEADER]).toBeUndefined();
  });

  it("lists devices with the control token when the browser is not paired", async () => {
    const { sent, fetcher } = recorder(ok({ devices: [] }));
    await listDevices("tok-1", fetcher);
    expect(headers(sent[0].init)[CONTROL_TOKEN_HEADER]).toBe("tok-1");
  });

  it("revokes only after the person confirms", async () => {
    const { sent, fetcher } = recorder(ok({ status: "ok" }));
    const prompts: string[] = [];
    expect(await confirmAndRevoke(sessionAuth, devices[1], (text) => { prompts.push(text); return false; }, fetcher)).toBe("cancelled");
    expect(sent).toHaveLength(0);
    expect(await confirmAndRevoke(sessionAuth, devices[1], () => true, fetcher)).toBe("revoked");
    expect(sent[0].url).toBe("/identity/v1/devices/dev_bbbb/revoke");
    expect(sent[0].init.method).toBe("POST");
    expect(headers(sent[0].init)[CSRF_HEADER]).toBe(CSRF);
    expect(prompts[0]).toContain("Old desktop (dev_bbbb)");
    expect(revokePrompt(devices[0])).toContain("browser you are using");
  });

  it("revokes with the control token, and refuses a malformed device id without sending", async () => {
    const { sent, fetcher } = recorder(ok({ status: "ok" }));
    await revokeDevice("tok-1", "dev_bbbb", fetcher);
    expect(headers(sent[0].init)[CONTROL_TOKEN_HEADER]).toBe("tok-1");
    expect(headers(sent[0].init)[CSRF_HEADER]).toBeUndefined();
    await expect(revokeDevice("tok-1", "../pairing-codes", fetcher)).rejects.toBeInstanceOf(HttpError);
    expect(sent).toHaveLength(1);
  });

  it("explains an unknown device", async () => {
    const { fetcher } = recorder(refusal(404, "DEVICE_NOT_FOUND"));
    const failure = await revokeDevice(sessionAuth, "dev_zzzz", fetcher).catch((error: unknown) => error);
    expect(identityFailure(failure)).toContain("not known");
  });

  it("shows the devices, marks this browser, and offers revoke only for active ones", () => {
    const html = renderToStaticMarkup(createElement(DeviceList, { devices, busy: false, onRevoke: () => undefined }));
    expect(html).toContain("This browser");
    expect(html).toContain("dev_aaaa");
    expect(html.match(/>Revoke</g)).toHaveLength(2);
    expect(html).toContain("Revoked ");
  });

  it("says when a session the page was using is gone", () => {
    const paired: IdentityView = { phase: "paired", session, providers: [] };
    expect(sessionEndedNotice(paired, { phase: "unpaired", providers: [] })).toContain("Pair this browser again");
    expect(sessionEndedNotice(paired, { phase: "disabled" })).toContain("PADOS_IDENTITY_SESSIONS_ENABLED");
    expect(sessionEndedNotice({ phase: "unpaired", providers: [] }, { phase: "unpaired", providers: [] })).toBeNull();
    expect(sessionEndedNotice(paired, paired)).toBeNull();
  });
});

describe("closed error codes", () => {
  it.each(["IDENTITY_DISABLED", "PAIRING_CODE_INVALID", "PAIRING_CODE_EXPIRED", "PAIRING_RATE_LIMITED", "SESSION_REQUIRED",
    "SESSION_EXPIRED", "SESSION_REVOKED", "CSRF_INVALID", "ORIGIN_NOT_ALLOWED", "DEVICE_NOT_FOUND", "DEVICE_LIMIT"])(
    "%s has an operator-facing message", (code) => {
      const message = identityFailure(new HttpError("SERVER_ERROR", 403, code, "r-9", false, "NO"));
      expect(message.length).toBeGreaterThan(code.length + 30);
      expect(message).toContain(`(${code}, request r-9)`);
    });

  it("re-pairing is the advice for an expired or revoked session", () => {
    expect(IDENTITY_MESSAGES.SESSION_EXPIRED).toContain("pados device pair");
    expect(IDENTITY_MESSAGES.SESSION_REVOKED).toContain("pados device pair");
  });

  it("only session codes make the page read the session again", () => {
    expect(isSessionLost(new HttpError("SERVER_ERROR", 401, "SESSION_EXPIRED", "", false, "NO"))).toBe(true);
    expect(isSessionLost(new HttpError("SERVER_ERROR", 409, "DEVICE_LIMIT", "", false, "NO"))).toBe(false);
    expect(isSessionLost(new Error("SESSION_EXPIRED"))).toBe(false);
  });
});

describe("what the Control Center shows", () => {
  function identity(view: IdentityView, notice = ""): Identity {
    const none = async () => null;
    return { view, notice, refresh: async () => undefined, pair: none, logout: none, sessionLost: () => undefined };
  }
  const render = (view: IdentityView, controlValue = "", notice = "") => renderToStaticMarkup(createElement(ControlAccessPanel,
    { identity: identity(view, notice), controlValue, onControlValue: () => undefined }));
  const format = (value?: string) => value || "—";

  it("paired: who the browser is, Log out, and no token box", () => {
    const html = render({ phase: "paired", session, providers: [] });
    expect(html).toContain("Paired as Edge on desk (device dev_0123456789abcdef), expires ");
    expect(html).toContain("Log out");
    expect(html).not.toContain('name="control-token"');
    expect(html).not.toContain("Pair this browser");
    expect(html).not.toContain(CSRF);  // the CSRF token is never rendered
  });

  it("not paired: the pairing form with the terminal instruction, and the token behind a disclosure", () => {
    const html = render({ phase: "unpaired", providers: [] });
    expect(html).toContain("Not paired");
    expect(html).toContain("Pair this browser");
    expect(html).toContain("<code>pados device pair</code> in a terminal on this computer");
    expect(html).toContain('placeholder="XXXXX-XXXXX"');
    expect(html).toContain("<details");
    expect(html).toContain("Use the control token instead");
    expect(html).not.toMatch(/<details[^>]*open/);
    expect(html).not.toContain("sign-in options");  // no providers, nothing shown
  });

  it("not paired with a token already typed: the disclosure stays open so it can be forgotten", () => {
    expect(render({ phase: "unpaired", providers: [] }, "tok-1")).toMatch(/<details[^>]*open/);
  });

  it("shows sign-in providers only when there are some", () => {
    const html = render({ phase: "unpaired", providers: [{ id: "entra", label: "Microsoft Entra ID" }] });
    expect(html).toContain("Microsoft Entra ID");
  });

  it("expired: says to pair again", () => {
    const html = render({ phase: "unpaired", providers: [] }, "", sessionEndedNotice(
      { phase: "paired", session, providers: [] }, { phase: "unpaired", providers: [] }) || "");
    expect(html).toContain("session has ended");
    expect(html).toContain("Pair this browser again");
  });

  it("disabled: how to turn pairing on, and the token box exactly as before", () => {
    const html = render({ phase: "disabled" });
    expect(html).toContain("set PADOS_IDENTITY_SESSIONS_ENABLED=true and restart pados serve");
    expect(html).toContain('name="control-token"');
    expect(html).toContain("Use token");
    expect(html).not.toContain("<details");
  });

  it("unavailable: the reason, and the token box", () => {
    const html = render({ phase: "unavailable", reason: "This server does not offer browser pairing. Use the control token." });
    expect(html).toContain("does not offer browser pairing");
    expect(html).toContain('name="control-token"');
  });

  it("states the status in plain words", () => {
    expect(identityStatusText({ phase: "unpaired", providers: [] }, format)).toBe("Not paired");
    expect(identityStatusText({ phase: "paired", session, providers: [] }, format))
      .toBe("Paired as Edge on desk (device dev_0123456789abcdef), expires 2026-11-17T10:00:00Z");
  });

  it("keeps the step-up guidance on the identity page", () => {
    const html = renderToStaticMarkup(createElement(IdentityPage, { identity: identity({ phase: "disabled" }), auth: "",
      controlValue: "", onControlValue: () => undefined }));
    expect(html).toContain("WebAuthn step-up");
    expect(html).toContain("Remote console");
    expect(html).not.toContain('aria-label="Devices"');  // no devices panel while pairing is off
  });

  it("asks to pair or enter the token on a write page with nothing to write with", () => {
    const html = renderToStaticMarkup(createElement(WorkRequestPage, { auth: "", projects: [], pairingOffered: true }));
    expect(html).toContain("Pair this browser or enter the control token first");
    const legacy = renderToStaticMarkup(createElement(WorkRequestPage, { auth: "", projects: [] }));
    expect(legacy).toContain("Enter the control token first");
  });
});

describe("nothing is kept in browser storage", () => {
  function spyStorage(): Storage {
    return { getItem: vi.fn(), setItem: vi.fn(), removeItem: vi.fn(), clear: vi.fn(), key: vi.fn(), length: 0 } as unknown as Storage;
  }

  it("stores neither the CSRF token nor the control token anywhere while pairing, using and ending a session", async () => {
    const local = spyStorage();
    const perSession = spyStorage();
    vi.stubGlobal("localStorage", local);
    vi.stubGlobal("sessionStorage", perSession);
    const logSpies = (["log", "info", "warn", "error", "debug"] as const).map((name) => vi.spyOn(console, name));
    const { fetcher } = recorder(ok({ ...pairedBody, status: "ok", approval, devices: [], work_request: { id: "w" } }));
    await loadIdentityView(fetcher);
    await pair("ABCDE-FGHJK", "Laptop", fetcher);
    await createWorkRequest(sessionAuth, "p1", "t", "b", fetcher);
    await createWorkRequest("tok-1", "p1", "t", "b", fetcher);
    await listDevices(sessionAuth, fetcher);
    await revokeDevice(sessionAuth, "dev_bbbb", fetcher);
    await logout(session, fetcher);
    for (const storage of [local, perSession]) {
      for (const method of ["getItem", "setItem", "removeItem", "clear", "key"] as const) {
        expect(storage[method]).not.toHaveBeenCalled();
      }
    }
    for (const spy of logSpies) expect(spy).not.toHaveBeenCalled();
  });

  it("has no storage, cookie or console access in the identity sources", () => {
    const sources = import.meta.glob(["./identity.ts", "./identity-panel.tsx", "./control-auth.ts", "./local-page.ts"],
      { query: "?raw", import: "default", eager: true }) as Record<string, string>;
    expect(Object.keys(sources)).toHaveLength(4);
    for (const [name, source] of Object.entries(sources)) {
      expect(source, name).not.toMatch(/localStorage|sessionStorage|indexedDB|document\.cookie|console\.(log|info|warn|error|debug|trace)\(|location\.(search|hash)/);
    }
  });
});

describe("the session path ignores caller headers that would change who is asking", () => {
  it("drops a caller's control token and CSRF value, and sets the session's CSRF last", () => {
    const init = authorizedInit(sessionAuth, { method: "POST", headers: { [CONTROL_TOKEN_HEADER]: "tok-1",
      "x-pados-csrf": "forged", "X-Extra": "1" } });
    expect(headers(init)).toEqual({ Accept: "application/json", "Content-Type": "application/json", "X-Extra": "1",
      [CSRF_HEADER]: CSRF });
  });

  it("accepts a Headers instance from a caller", () => {
    const init = authorizedInit(sessionAuth, { method: "GET", headers: new Headers({ "X-Extra": "1", [CONTROL_TOKEN_HEADER]: "t" }) });
    expect(headers(init)).toEqual({ Accept: "application/json", "Content-Type": "application/json", "x-extra": "1" });
  });
});
