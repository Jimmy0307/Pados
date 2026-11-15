// SPDX-License-Identifier: AGPL-3.0-only
// Single sign-on (OIDC) in the Control Center: Unified Identity & Session phase 2
// (docs/identity/UNIFIED_IDENTITY_SESSION_DESIGN.md §4 and §3.10).
// - The sign-in section lists the providers GET /identity/v1/session reports, each with its server-made start link.
// - The server's redirect back (?sso=ok or ?sso_error=<CODE>) is read once on load, shown, and removed from the
//   address bar with history.replaceState, so a reload or a shared link never repeats it.
// - With an actor session, GET /identity/v1/me says who signed in and the per-project roles. The actor CSRF token
//   it returns stays in page memory (the caller's state) and is sent only to end that session.
// An SSO sign-in never grants the control token's authority, never pairs a browser, and never accepts a result.
// Nothing here touches browser storage, cookies (the session cookie is HttpOnly) or the console.
import { HttpError, describeFailure, requestJson } from "./http";
import { isLocalPage } from "./local-page";
import { CSRF_HEADER, SAME_ORIGIN_COOKIES } from "./control-auth";

export const SSO_OK_PARAM = "sso";
export const SSO_ERROR_PARAM = "sso_error";
export const SSO_RESPONSE_INVALID = "SSO_RESPONSE_INVALID";
const IDENTITY = "/identity/v1";
const START_URL = /^\/identity\/v1\/oidc\/[a-z0-9][a-z0-9-]{0,31}\/start$/;
const CLOSED_CODE = /^[A-Z][A-Z0-9_]{1,63}$/;

export type SsoProvider = { id: string; label: string; startUrl: string };
export type ProjectRoles = { projectId: string; roles: string[] };
export type ActorProfile = {
  actorId: string;
  displayName: string;
  providerId: string;
  issuer: string;
  issuerHost: string;
  projects: ProjectRoles[];
  expiresAt?: string;
  csrfToken: string;
};
/** What GET /identity/v1/session says about single sign-on. */
export type SsoState = { enabled: boolean; providers: SsoProvider[]; actorPresent: boolean };
/** The redirect back from a provider, as read from the address once. */
export type SsoReturn = { kind: "ok" } | { kind: "error"; code: string };

// zh-TW first: the person reads why; the closed code is in the expandable detail.
export const SSO_OK_MESSAGE = "已透過單一登入（SSO）完成登入。";
export const SSO_GENERIC_ERROR = "單一登入未完成，未建立任何登入工作階段。";
export const SSO_MESSAGES: Readonly<Record<string, string>> = {
  SSO_IDENTITY_NOT_BOUND: "身分提供者已確認您的身分，但這個身分尚未綁定到任何 PADOS 使用者。請管理員在 PADOS_IDENTITY_ACTORS_CONFIG 中加入您的 issuer 與 subject。",
  OIDC_STATE_INVALID: "這次登入已過期，或是在另一個瀏覽器開始的。請從這個頁面重新登入。",
  OIDC_TOKEN_INVALID: "身分提供者回傳的身分權杖未通過驗證，因此沒有登入。",
  OIDC_PROVIDER_ERROR: "身分提供者拒絕或取消了這次登入。",
  OIDC_PROVIDER_UNAVAILABLE: "目前無法連線到身分提供者，請稍後再試。",
  OIDC_PROVIDER_NOT_CONFIGURED: "這個登入方式沒有在伺服器上設定。",
  OIDC_CONFIG_INVALID: "伺服器的單一登入設定無效，請管理員查看伺服器日誌。",
  ACTORS_CONFIG_INVALID: "伺服器的使用者綁定設定無效，請管理員查看伺服器日誌。",
  OIDC_START_RATE_LIMITED: "登入嘗試太頻繁，請稍候再試。",
  IDENTITY_DISABLED: "這個伺服器沒有啟用瀏覽器登入工作階段。",
  ORIGIN_NOT_ALLOWED: "伺服器不接受這個頁面的來源，請從 http://127.0.0.1:5173 或 http://127.0.0.1:8088 開啟。",
  IDENTITY_STORE_CORRUPT: "伺服器的身分資料無法使用，請管理員處理。",
  IDENTITY_STORE_INSECURE: "伺服器的身分資料存放位置權限不安全，已拒絕使用，請管理員處理。",
  SESSION_EXPIRED: "單一登入工作階段已過期，請重新登入。",
  SESSION_REVOKED: "單一登入工作階段已結束，請重新登入。",
  SESSION_REQUIRED: "目前沒有單一登入工作階段。",
};

export function ssoMessage(code: string): string {
  return SSO_MESSAGES[code] ?? SSO_GENERIC_ERROR;
}

// ---- the redirect back from the provider ----------------------------------------------------------------------

/** ``?sso=ok`` or ``?sso_error=<CODE>``; anything else (or nothing) is no return. A malformed code is not echoed. */
export function readSsoReturn(search: string): SsoReturn | null {
  const params = new URLSearchParams(search);
  const errors = params.getAll(SSO_ERROR_PARAM);
  if (errors.length) {
    const code = errors.length === 1 && CLOSED_CODE.test(errors[0]) ? errors[0] : "SSO_ERROR_UNRECOGNIZED";
    return { kind: "error", code };
  }
  return params.getAll(SSO_OK_PARAM).length === 1 && params.get(SSO_OK_PARAM) === "ok" ? { kind: "ok" } : null;
}

/** The same address without the two SSO parameters (every other parameter and the fragment are kept). */
export function addressWithoutSsoReturn(pathname: string, search: string, hash: string): string {
  const params = new URLSearchParams(search);
  params.delete(SSO_OK_PARAM);
  params.delete(SSO_ERROR_PARAM);
  const rest = params.toString();
  return `${pathname}${rest ? `?${rest}` : ""}${hash}`;
}

type AddressLike = { pathname: string; search: string; hash: string };
type HistoryLike = { state: unknown; replaceState: (data: unknown, unused: string, url?: string) => void };

/** Read the return once and clean the address bar, so a reload or a copied link never shows it again. */
export function consumeSsoReturn(address: AddressLike | undefined = globalThis.location,
                                 history: HistoryLike | undefined = globalThis.history): SsoReturn | null {
  if (!address) return null;
  const found = readSsoReturn(address.search);
  if (found && history) history.replaceState(history.state, "", addressWithoutSsoReturn(address.pathname, address.search, address.hash));
  return found;
}

export function ssoReturnMessage(found: SsoReturn): string {
  return found.kind === "ok" ? SSO_OK_MESSAGE : ssoMessage(found.code);
}

// ---- response parsing (checked, not trusted) ----------------------------------------------------------------

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function invalid(what: string): Error {
  return new Error(`${SSO_RESPONSE_INVALID}: ${what}`);
}

/** Providers with a usable start link: a same-origin path of the exact /identity/v1/oidc/<id>/start shape. */
export function parseSsoProviders(value: unknown): SsoProvider[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): SsoProvider[] => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    const id = text(record.id);
    const startUrl = text(record.start_url);
    if (!id || !startUrl || !START_URL.test(startUrl) || startUrl !== `${IDENTITY}/oidc/${id}/start`) return [];
    return [{ id, label: text(record.display_name) ?? id, startUrl }];
  });
}

export function parseSsoState(body: unknown): SsoState {
  if (!body || typeof body !== "object") throw invalid("session");
  const record = body as Record<string, unknown>;
  if (typeof record.enabled !== "boolean") throw invalid("session.enabled");
  if (!record.enabled) return { enabled: false, providers: [], actorPresent: false };
  const actor = record.actor;
  const actorPresent = Boolean(actor && typeof actor === "object" && (actor as Record<string, unknown>).auth_method === "oidc");
  return { enabled: true, providers: parseSsoProviders(record.providers), actorPresent };
}

export function issuerHost(issuer: string): string {
  try {
    return new URL(issuer).host || issuer;
  } catch {
    return issuer;
  }
}

export function parseActorProfile(body: unknown): ActorProfile {
  const record = (body && typeof body === "object" ? body : {}) as Record<string, unknown>;
  const actorId = text(record.actor_id);
  const csrfToken = text(record.csrf_token);
  const issuer = text(record.issuer);
  if (!actorId || !csrfToken || !issuer) throw invalid("me");
  const projects = Array.isArray(record.projects) ? record.projects.flatMap((item): ProjectRoles[] => {
    if (!item || typeof item !== "object") return [];
    const entry = item as Record<string, unknown>;
    const projectId = text(entry.project_id);
    const roles = Array.isArray(entry.roles) ? entry.roles.filter((role): role is string => typeof role === "string" && role.length > 0) : [];
    return projectId ? [{ projectId, roles }] : [];
  }) : [];
  return {
    actorId,
    displayName: text(record.display_name) ?? actorId,
    providerId: text(record.provider_id) ?? "",
    issuer,
    issuerHost: issuerHost(issuer),
    projects,
    expiresAt: text(record.expires_at),
    csrfToken,
  };
}

// ---- the client ---------------------------------------------------------------------------------------------

function ssoRequest<T>(path: string, init: RequestInit, fetcher?: typeof fetch): Promise<T> {
  if (!isLocalPage()) return Promise.reject(new Error("SSO_LOCAL_ONLY: open the console on this computer (127.0.0.1)"));
  return requestJson<T>(`${IDENTITY}${path}`, { ...init, ...SAME_ORIGIN_COOKIES }, fetcher ? { fetcher } : {});
}

export async function getSsoState(fetcher?: typeof fetch): Promise<SsoState> {
  return parseSsoState(await ssoRequest<unknown>("/session", { method: "GET", headers: { Accept: "application/json" } }, fetcher));
}

/** GET /identity/v1/me: the signed-in actor, still bound; refused when there is no actor session. */
export async function getActorProfile(fetcher?: typeof fetch): Promise<ActorProfile> {
  return parseActorProfile(await ssoRequest<unknown>("/me", { method: "GET", headers: { Accept: "application/json" } }, fetcher));
}

/** POST /identity/v1/actor/logout: ends only the actor session (a paired browser session is not touched). */
export async function logoutActor(profile: ActorProfile, fetcher?: typeof fetch): Promise<void> {
  await ssoRequest<unknown>("/actor/logout", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json", [CSRF_HEADER]: profile.csrfToken },
    body: "{}",
  }, fetcher);
}

// ---- what the section shows ---------------------------------------------------------------------------------

export type SsoView =
  | { phase: "loading" }
  | { phase: "hidden" }
  | { phase: "signed-out"; providers: SsoProvider[] }
  | { phase: "signed-in"; profile: ActorProfile; providers: SsoProvider[] }
  | { phase: "unavailable"; reason: string };

/** Read the session and, with an actor session, the actor; the section is hidden when SSO offers nothing. */
export async function loadSsoView(fetcher?: typeof fetch): Promise<SsoView> {
  if (!isLocalPage()) return { phase: "hidden" };
  let state: SsoState;
  try {
    state = await getSsoState(fetcher);
  } catch (error) {
    if (error instanceof HttpError && error.status === 404) return { phase: "hidden" };  // an older server
    return { phase: "unavailable", reason: ssoFailure(error) };
  }
  if (!state.enabled) return { phase: "hidden" };
  if (state.actorPresent) {
    try {
      return { phase: "signed-in", profile: await getActorProfile(fetcher), providers: state.providers };
    } catch (error) {
      // A session the server no longer binds (binding or provider removed) is simply not signed in.
      if (!(error instanceof HttpError) || error.status !== 403) return { phase: "unavailable", reason: ssoFailure(error) };
    }
  }
  return state.providers.length ? { phase: "signed-out", providers: state.providers } : { phase: "hidden" };
}

export function ssoFailure(error: unknown): string {
  if (error instanceof HttpError) return `${ssoMessage(error.code)} (${error.code})`;
  return describeFailure(error);
}

export const ROLE_LABELS: Readonly<Record<string, string>> = {
  viewer: "檢視者（viewer）",
  contributor: "貢獻者（contributor）：可建立工作請求",
  approver: "核准者（approver）",
};
