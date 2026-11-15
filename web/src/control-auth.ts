// SPDX-License-Identifier: AGPL-3.0-only
// How one /control (or /identity device) request proves its caller. Two paths, never both on one request:
// - the control token path, unchanged: the token typed into this page travels only in X-PADOS-Control-Token;
// - the paired browser session path: no control token at all. The HttpOnly session cookie travels by itself
//   (same-origin only), and every state-changing request adds the session's CSRF token in X-PADOS-CSRF.
// The CSRF token is held in page memory only, like the control token, and never in browser storage, a URL or a log.
export const CONTROL_TOKEN_HEADER = "X-PADOS-Control-Token";
export const CSRF_HEADER = "X-PADOS-CSRF";
/** The session cookie is sent to this page's own origin only (the Vite dev server proxies to the API unchanged). */
export const SAME_ORIGIN_COOKIES = { credentials: "same-origin" } as const satisfies RequestInit;

/** The paired browser session as a request needs it: only the CSRF token (the cookie is HttpOnly, out of reach). */
export type SessionAuth = { readonly kind: "session"; readonly csrfToken: string; readonly deviceId?: string };
/** A control token typed into this page (string, possibly empty), or this browser's paired session. */
export type ControlAuth = string | SessionAuth;

const STATE_CHANGING = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function isSessionAuth(auth: ControlAuth): auth is SessionAuth {
  return typeof auth === "object" && auth !== null && auth.kind === "session";
}

/** Whether this page can make a change at all: a paired session, or a non-empty control token. */
export function hasControlAuth(auth: ControlAuth): boolean {
  return isSessionAuth(auth) ? Boolean(auth.csrfToken) : Boolean(auth);
}

export function isStateChanging(method: string | undefined): boolean {
  return STATE_CHANGING.has((method || "GET").toUpperCase());
}

/** What the person confirms a decision is recorded with. */
export function authDescription(auth: ControlAuth): string {
  if (!isSessionAuth(auth)) return "the control token";
  return auth.deviceId ? `this browser's paired session (device ${auth.deviceId})` : "this browser's paired session";
}

/** The request init for one call on either path; the caller's own headers win, as before. */
export function authorizedInit(auth: ControlAuth, init: RequestInit = {}): RequestInit {
  const base = { Accept: "application/json", "Content-Type": "application/json" };
  if (!isSessionAuth(auth)) {
    // The control token path exactly as it was: the token header, no change to the cookie mode. A session cookie the
    // browser may still hold travels too (the default same-origin mode), but the server lets the header alone decide.
    return { ...init, headers: { ...base, [CONTROL_TOKEN_HEADER]: auth, ...(init.headers || {}) } };
  }
  // The session path: a caller's headers never add a control token (which would replace the cookie) or their own
  // CSRF value; the session's CSRF token is set last, on state-changing methods only.
  const reserved = new Set([CONTROL_TOKEN_HEADER.toLowerCase(), CSRF_HEADER.toLowerCase()]);
  const caller = Object.entries(plainHeaders(init.headers)).filter(([name]) => !reserved.has(name.toLowerCase()));
  const csrf: Record<string, string> = isStateChanging(init.method) ? { [CSRF_HEADER]: auth.csrfToken } : {};
  return { ...init, ...SAME_ORIGIN_COOKIES, headers: { ...base, ...Object.fromEntries(caller), ...csrf } };
}

function plainHeaders(headers: HeadersInit | undefined): Record<string, string> {
  if (!headers) return {};
  if (headers instanceof Headers || Array.isArray(headers)) return Object.fromEntries(new Headers(headers).entries());
  return { ...headers };
}
