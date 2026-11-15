// SPDX-License-Identifier: AGPL-3.0-only
// Single sign-on in the Control Center (sso.ts, sso-panel.tsx; docs/identity/UNIFIED_IDENTITY_SESSION_DESIGN.md §3.10):
// providers with server-made start links, hidden when there are none; the provider's redirect back (?sso=ok or
// ?sso_error=<CODE>) is shown in zh-TW with the code in a detail and removed from the address bar; the signed-in actor
// and its per-project roles from GET /identity/v1/me; an actor-only logout; nothing in browser storage.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CSRF_HEADER, SAME_ORIGIN_COOKIES } from "./control-auth";
import {
  SSO_GENERIC_ERROR,
  SSO_OK_MESSAGE,
  addressWithoutSsoReturn,
  consumeSsoReturn,
  getActorProfile,
  issuerHost,
  loadSsoView,
  logoutActor,
  parseSsoProviders,
  readSsoReturn,
  ssoMessage,
  type ActorProfile,
} from "./sso";
import { SsoSection } from "./sso-panel";

type Sent = { url: string; init: RequestInit };
type Reply = { status: number; body: unknown };

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
const refusal = (status: number, code: string): Reply => ({ status, body: {
  error: { schema_version: "pados.error.v1", code, category: "AUTH", message: code, retryable: false, request_id: "srv-1",
    write_occurred: "NO" } } });

const ACTOR_CSRF = "actor-csrf-derived-value";
const entra = { id: "entra", kind: "entra", display_name: "Microsoft Entra ID", start_url: "/identity/v1/oidc/entra/start" };
const google = { id: "google", kind: "google", display_name: "Google", start_url: "/identity/v1/oidc/google/start" };
const signedOutSession = { enabled: true, authenticated: false, providers: [entra, google] };
const actorSession = { ...signedOutSession, actor: { auth_method: "oidc", actor_id: "alice", provider_id: "entra",
  issuer: "https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/v2.0", subject: "sub-1" } };
const me = {
  actor_id: "alice", display_name: "Alice Chen", auth_method: "oidc", provider_id: "entra",
  issuer: "https://login.microsoftonline.com/11111111-2222-3333-4444-555555555555/v2.0", subject: "sub-1",
  projects: [{ project_id: "proj-a", roles: ["viewer", "contributor"] }, { project_id: "proj-b", roles: ["approver"] }],
  expires_at: "2026-11-17T10:00:00Z", idle_expires_at: "2026-11-16T12:00:00Z", csrf_token: ACTOR_CSRF,
};
const profile: ActorProfile = {
  actorId: "alice", displayName: "Alice Chen", providerId: "entra", issuer: me.issuer,
  issuerHost: "login.microsoftonline.com", projects: [{ projectId: "proj-a", roles: ["viewer", "contributor"] }],
  expiresAt: me.expires_at, csrfToken: ACTOR_CSRF,
};

beforeEach(() => { vi.stubGlobal("location", { hostname: "127.0.0.1" }); });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("the provider's redirect back", () => {
  it.each<[string, ReturnType<typeof readSsoReturn>]>([
    ["?sso=ok", { kind: "ok" }],
    ["?sso_error=SSO_IDENTITY_NOT_BOUND", { kind: "error", code: "SSO_IDENTITY_NOT_BOUND" }],
    ["?view=x&sso_error=OIDC_STATE_INVALID", { kind: "error", code: "OIDC_STATE_INVALID" }],
    ["?sso_error=<script>", { kind: "error", code: "SSO_ERROR_UNRECOGNIZED" }],
    ["?sso_error=A&sso_error=B", { kind: "error", code: "SSO_ERROR_UNRECOGNIZED" }],
    ["?sso=yes", null],
    ["?sso=ok&sso=ok", null],
    ["", null],
  ])("reads %s", (search, expected) => {
    expect(readSsoReturn(search)).toEqual(expected);
  });

  it("removes only the SSO parameters and keeps the rest of the address", () => {
    expect(addressWithoutSsoReturn("/operator.html", "?sso=ok", "")).toBe("/operator.html");
    expect(addressWithoutSsoReturn("/Pados/operator.html", "?view=actions&sso_error=X_Y", "#top")).toBe("/Pados/operator.html?view=actions#top");
  });

  it("cleans the address bar with history.replaceState once it has read the return", () => {
    const replaceState = vi.fn();
    const found = consumeSsoReturn({ pathname: "/operator.html", search: "?sso_error=OIDC_TOKEN_INVALID", hash: "" },
      { state: { keep: 1 }, replaceState });
    expect(found).toEqual({ kind: "error", code: "OIDC_TOKEN_INVALID" });
    expect(replaceState).toHaveBeenCalledWith({ keep: 1 }, "", "/operator.html");
    const untouched = vi.fn();
    expect(consumeSsoReturn({ pathname: "/operator.html", search: "?view=x", hash: "" }, { state: null, replaceState: untouched })).toBeNull();
    expect(untouched).not.toHaveBeenCalled();
  });

  it("explains every closed code in zh-TW and falls back for an unknown one", () => {
    expect(ssoMessage("SSO_IDENTITY_NOT_BOUND")).toContain("尚未綁定");
    expect(ssoMessage("OIDC_STATE_INVALID")).toContain("重新登入");
    expect(ssoMessage("SOMETHING_NEW")).toBe(SSO_GENERIC_ERROR);
  });
});

describe("providers and the actor from the server", () => {
  it("keeps only providers whose start link is the exact same-origin start path", () => {
    expect(parseSsoProviders([entra, google])).toEqual([
      { id: "entra", label: "Microsoft Entra ID", startUrl: "/identity/v1/oidc/entra/start" },
      { id: "google", label: "Google", startUrl: "/identity/v1/oidc/google/start" }]);
    for (const startUrl of ["https://evil.example/identity/v1/oidc/entra/start", "javascript:alert(1)",
      "//evil.example/identity/v1/oidc/entra/start", "/identity/v1/oidc/google/start", "/identity/v1/oidc/entra/start?x=1"]) {
      expect(parseSsoProviders([{ ...entra, start_url: startUrl }]), startUrl).toEqual([]);
    }
    expect(parseSsoProviders(["entra", 7, null])).toEqual([]);
    expect(parseSsoProviders(undefined)).toEqual([]);
  });

  it("is hidden when the server offers no provider, pairing is off, or the server is older", async () => {
    expect(await loadSsoView(recorder(ok({ enabled: true, authenticated: false, providers: [] })).fetcher)).toEqual({ phase: "hidden" });
    expect(await loadSsoView(recorder(ok({ enabled: false, authenticated: false, providers: [entra] })).fetcher)).toEqual({ phase: "hidden" });
    expect(await loadSsoView(recorder(refusal(404, "HTTP_404")).fetcher)).toEqual({ phase: "hidden" });
  });

  it("offers the providers when signed out, with one same-origin GET and no CSRF", async () => {
    const { sent, fetcher } = recorder(ok(signedOutSession));
    const view = await loadSsoView(fetcher);
    expect(view.phase).toBe("signed-out");
    expect(sent.map((call) => call.url)).toEqual(["/identity/v1/session"]);
    expect(sent[0].init).toMatchObject(SAME_ORIGIN_COOKIES);
    expect((sent[0].init.headers as Record<string, string>)[CSRF_HEADER]).toBeUndefined();
  });

  it("reads the actor and its roles from /me when an actor session exists", async () => {
    const { sent, fetcher } = recorder(ok(actorSession), ok(me));
    const view = await loadSsoView(fetcher);
    expect(sent.map((call) => call.url)).toEqual(["/identity/v1/session", "/identity/v1/me"]);
    expect(view).toMatchObject({ phase: "signed-in", profile: { actorId: "alice", displayName: "Alice Chen",
      providerId: "entra", issuerHost: "login.microsoftonline.com", csrfToken: ACTOR_CSRF,
      projects: [{ projectId: "proj-a", roles: ["viewer", "contributor"] }, { projectId: "proj-b", roles: ["approver"] }] } });
  });

  it("treats an actor the server no longer binds as signed out", async () => {
    const view = await loadSsoView(recorder(ok(actorSession), refusal(403, "SSO_IDENTITY_NOT_BOUND")).fetcher);
    expect(view.phase).toBe("signed-out");
  });

  it("refuses a /me answer without the actor, issuer or CSRF token", async () => {
    await expect(getActorProfile(recorder(ok({ ...me, csrf_token: "" })).fetcher)).rejects.toThrow(/SSO_RESPONSE_INVALID/);
  });

  it("shows the issuer's host, or the issuer itself when it is not a URL", () => {
    expect(issuerHost("https://accounts.google.com")).toBe("accounts.google.com");
    expect(issuerHost("accounts.google.com")).toBe("accounts.google.com");
  });

  it("asks nothing from a page served by another host", async () => {
    vi.stubGlobal("location", { hostname: "jimmy0307.github.io" });
    const { sent, fetcher } = recorder(ok(actorSession));
    expect(await loadSsoView(fetcher)).toEqual({ phase: "hidden" });
    await expect(logoutActor(profile, fetcher)).rejects.toThrow(/SSO_LOCAL_ONLY/);
    expect(sent).toHaveLength(0);
  });
});

describe("ending the actor session", () => {
  it("posts to the actor-only logout with the actor's CSRF token and no control token", async () => {
    const { sent, fetcher } = recorder(ok({ status: "ok", authenticated: false }));
    await logoutActor(profile, fetcher);
    expect(sent[0].url).toBe("/identity/v1/actor/logout");
    expect(sent[0].init.method).toBe("POST");
    expect(sent[0].init).toMatchObject(SAME_ORIGIN_COOKIES);
    const headers = sent[0].init.headers as Record<string, string>;
    expect(headers[CSRF_HEADER]).toBe(ACTOR_CSRF);
    expect(headers["X-PADOS-Control-Token"]).toBeUndefined();
  });
});

describe("what the SSO section shows", () => {
  const render = (props: Parameters<typeof SsoSection>[0]) => renderToStaticMarkup(createElement(SsoSection, props));
  const providers = parseSsoProviders([entra, google]);

  it("hidden: renders nothing at all when there is nothing to offer or say", () => {
    expect(render({ view: { phase: "hidden" }, returned: null })).toBe("");
    expect(render({ view: { phase: "loading" }, returned: null })).toBe("");
  });

  it("signed out: a sign-in link per provider, to the server's start path", () => {
    const html = render({ view: { phase: "signed-out", providers }, returned: null });
    expect(html).toContain('href="/identity/v1/oidc/entra/start"');
    expect(html).toContain("使用 Microsoft Entra ID 登入");
    expect(html).toContain('href="/identity/v1/oidc/google/start"');
    expect(html).toContain("WebAuthn");  // SSO never accepts a result
  });

  it("signed in: who, which provider, the issuer host and the roles table, with a logout; never the CSRF token", () => {
    const html = render({ view: { phase: "signed-in", profile, providers }, returned: null });
    expect(html).toContain("Alice Chen");
    expect(html).toContain("login.microsoftonline.com");
    expect(html).toContain("entra");
    expect(html).toContain("proj-a");
    expect(html).toContain("貢獻者（contributor）");
    expect(html).toContain("登出單一登入");
    expect(html).not.toContain(ACTOR_CSRF);
    expect(html).not.toContain("/identity/v1/oidc/entra/start");  // no sign-in link while signed in
  });

  it("a successful return says so", () => {
    expect(render({ view: { phase: "signed-in", profile, providers }, returned: { kind: "ok" } })).toContain(SSO_OK_MESSAGE);
  });

  it("a refused return explains in zh-TW and keeps the code in an expandable detail, even with nothing else to show", () => {
    const html = render({ view: { phase: "hidden" }, returned: { kind: "error", code: "SSO_IDENTITY_NOT_BOUND" } });
    expect(html).toContain("尚未綁定");
    expect(html).toMatch(/<details><summary>技術細節（Details）<\/summary><p>錯誤代碼：<code>SSO_IDENTITY_NOT_BOUND<\/code>/);
    expect(html).toContain('role="alert"');
  });
});

describe("nothing is kept in browser storage", () => {
  it("has no storage, cookie or console access in the SSO sources", () => {
    const sources = import.meta.glob(["./sso.ts", "./sso-panel.tsx"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;
    expect(Object.keys(sources)).toHaveLength(2);
    for (const [name, source] of Object.entries(sources)) {
      expect(source, name).not.toMatch(/localStorage|sessionStorage|indexedDB|document\.cookie|console\.(log|info|warn|error|debug|trace)\(/);
    }
  });

  it("stores neither CSRF token while reading, signing in and logging out", async () => {
    const spy = { getItem: vi.fn(), setItem: vi.fn(), removeItem: vi.fn(), clear: vi.fn(), key: vi.fn(), length: 0 };
    vi.stubGlobal("localStorage", spy);
    vi.stubGlobal("sessionStorage", spy);
    const { fetcher } = recorder(ok(actorSession), ok(me), ok({ status: "ok" }));
    const view = await loadSsoView(fetcher);
    if (view.phase === "signed-in") await logoutActor(view.profile, fetcher);
    for (const method of ["getItem", "setItem", "removeItem", "clear", "key"] as const) expect(spy[method]).not.toHaveBeenCalled();
  });
});
