// SPDX-License-Identifier: AGPL-3.0-only
// The browser side of PADOS Unified Identity & Session, phase 1 (docs/identity/UNIFIED_IDENTITY_SESSION_DESIGN.md
// §3): pair this browser once with a one-time code from `pados device pair`, then restore the server-side session on
// every reload. The browser never sees the raw control token on this path and never sees the session id (an HttpOnly
// cookie). The only value it holds is the CSRF token the server returns, kept in page memory by the caller: never in
// browser storage, a URL or a log. A session grants exactly the control token's authority, never result acceptance,
// which stays with the Remote console's WebAuthn step-up.
import { HttpError, describeFailure, requestJson } from "./http";
import { isLocalPage } from "./local-page";
import { SAME_ORIGIN_COOKIES, authorizedInit, type ControlAuth, type SessionAuth } from "./control-auth";

export const IDENTITY_PATH = "/identity/v1";
export const IDENTITY_LOCAL_ONLY = "IDENTITY_LOCAL_ONLY";
export const IDENTITY_RESPONSE_INVALID = "IDENTITY_RESPONSE_INVALID";
export const PAIR_COMMAND = "pados device pair";
export const ENABLE_INSTRUCTION = "set PADOS_IDENTITY_SESSIONS_ENABLED=true and restart pados serve";
export const STEP_UP_CODE = "RESULT_ACCEPTANCE_REQUIRES_STEP_UP";
export const PAIRING_CODE_INCOMPLETE = "PAIRING_CODE_INCOMPLETE";
export const PAIRING_CODE_PATTERN = /^[0-9A-Z]{5}-[0-9A-Z]{5}$/;
export const DEVICE_LABEL_MAX_LENGTH = 64;

/** One provider offered for sign-in (phase 2 SSO). Phase 1 servers send none, and the page then shows none. */
export type IdentityProvider = { id: string; label: string };

/** The current paired session. ``csrfToken`` lives in page memory only and is sent only on state-changing calls. */
export type ActiveSession = {
  deviceId: string;
  deviceLabel: string;
  authMethod: string;
  expiresAt?: string;
  idleExpiresAt?: string;
  csrfToken: string;
};

/** What GET /identity/v1/session says about this browser. */
export type SessionState = { enabled: boolean; session: ActiveSession | null; providers: IdentityProvider[] };

export type Device = {
  deviceId: string;
  deviceLabel: string;
  pairedAt?: string;
  lastSeenAt?: string;
  revokedAt?: string;
  current: boolean;
};

// Operator-facing text for every closed code of the contract (§3.5), plus the step-up refusal the control API keeps.
export const IDENTITY_MESSAGES: Readonly<Record<string, string>> = {
  IDENTITY_DISABLED: `Browser pairing is turned off on this server. To use it, ${ENABLE_INSTRUCTION}; until then, use the control token.`,
  PAIRING_CODE_INVALID: `That pairing code was not accepted. Check it, or run \`${PAIR_COMMAND}\` again for a new code.`,
  [PAIRING_CODE_INCOMPLETE]: "Enter the whole 10-character code, as XXXXX-XXXXX. Nothing was sent.",
  PAIRING_CODE_EXPIRED: `That pairing code has expired (codes last 5 minutes). Run \`${PAIR_COMMAND}\` again for a new code.`,
  PAIRING_RATE_LIMITED: "Pairing is paused after too many failed attempts. Wait about 10 minutes, then try again with a new code.",
  SESSION_REQUIRED: "This browser is not paired. Pair it with a code, or use the control token.",
  SESSION_EXPIRED: `This browser's session has expired. Pair this browser again: run \`${PAIR_COMMAND}\` for a new code.`,
  SESSION_REVOKED: `This browser's session was revoked (logged out, or the device was revoked). Pair this browser again: run \`${PAIR_COMMAND}\` for a new code.`,
  CSRF_INVALID: "The server refused the request because its CSRF check failed. The session was checked again; review the page, then try again.",
  ORIGIN_NOT_ALLOWED: "The server refused this page's origin. Open the Control Center at http://127.0.0.1:5173 or http://127.0.0.1:8088, or add this origin to PADOS_IDENTITY_ALLOWED_ORIGINS.",
  DEVICE_NOT_FOUND: "That device is not known (it may already have been removed). The device list was reloaded.",
  DEVICE_LIMIT: "The device limit was reached. Revoke a device you no longer use, then try again.",
  [STEP_UP_CODE]: "Accepting a verified result needs a fresh WebAuthn step-up in the Remote console. Neither this browser's session nor the control token can accept it here.",
};

/** Codes after which the page's idea of the session is wrong: the session state is read again, nothing is retried. */
const SESSION_LOST_CODES = new Set(["SESSION_REQUIRED", "SESSION_EXPIRED", "SESSION_REVOKED", "CSRF_INVALID", "IDENTITY_DISABLED"]);

export function isSessionLost(error: unknown): boolean {
  return error instanceof HttpError && SESSION_LOST_CODES.has(error.code);
}

/** A clear message for a failed identity (or session-authenticated control) call. */
export function identityFailure(error: unknown): string {
  if (error instanceof Error && error.message.startsWith(IDENTITY_LOCAL_ONLY)) {
    return "Pairing works only from the console on this computer (http://127.0.0.1).";
  }
  if (!(error instanceof HttpError)) return describeFailure(error);
  const message = IDENTITY_MESSAGES[error.code];
  if (!message) return describeFailure(error);
  const reference = error.requestId ? `, request ${error.requestId}` : "";
  const unknown = error.writeOccurred === "UNKNOWN" && error.outcome === "WRITE_OUTCOME_UNKNOWN"
    ? " Refresh and check whether the change was applied before trying again." : "";
  return `${message}${unknown} (${error.code}${reference})`;
}

/** Shown when a pairing answer never reached the page (connection dropped, dev server restarted, page interrupted). */
export const PAIR_ANSWER_LOST = `The pairing answer did not reach this page, so the code may already have been used (each code works once). If this browser is not shown as paired below, run \`${PAIR_COMMAND}\` for a new code and pair again.`;

/** identityFailure for POST /pair: a lost answer means "get a new code", since a single-use code cannot be replayed. */
export function pairFailure(error: unknown): string {
  if (error instanceof HttpError && error.outcome === "WRITE_OUTCOME_UNKNOWN") {
    return `${PAIR_ANSWER_LOST}${error.requestId ? ` (request ${error.requestId})` : ""}`;
  }
  return identityFailure(error);
}

// ---- input normalisation -------------------------------------------------------------------------------------

/** Upper-case, drop spaces and any other separator, keep 10 characters, and group them as XXXXX-XXXXX. */
export function normalizePairingCode(raw: string): string {
  const chars = String(raw || "").toUpperCase().replace(/[^0-9A-Z]/g, "").slice(0, 10);
  return chars.length > 5 ? `${chars.slice(0, 5)}-${chars.slice(5)}` : chars;
}

export function isCompletePairingCode(raw: string): boolean {
  return PAIRING_CODE_PATTERN.test(normalizePairingCode(raw));
}

/** Printable characters only, single spaces, at most 64 characters (the server sanitizes again). */
export function sanitizeDeviceLabel(raw: string): string {
  return Array.from(String(raw || "").replace(/[\u0000-\u001f\u007f-\u009f]/g, " ").replace(/\s+/g, " ").trim())
    .slice(0, DEVICE_LABEL_MAX_LENGTH).join("").trim();
}

/** A label for a browser the person did not name: the browser family, never anything identifying. */
export function defaultDeviceLabel(userAgent: string = globalThis.navigator?.userAgent || ""): string {
  if (/Edg\//.test(userAgent)) return "Edge browser";
  if (/Firefox\//.test(userAgent)) return "Firefox browser";
  if (/Chrome\//.test(userAgent)) return "Chrome browser";
  if (/Safari\//.test(userAgent)) return "Safari browser";
  return "Browser";
}

// ---- response parsing (the server's answers are checked, not trusted) ----------------------------------------

function text(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function invalid(what: string): Error {
  return new Error(`${IDENTITY_RESPONSE_INVALID}: ${what}`);
}

function parseProviders(value: unknown): IdentityProvider[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item): IdentityProvider[] => {
    if (typeof item === "string" && item.trim()) return [{ id: item, label: item }];
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    const id = text(record.id) ?? text(record.provider_id) ?? text(record.name);
    const label = text(record.display_name) ?? text(record.label) ?? text(record.name) ?? id;
    return id && label ? [{ id, label }] : [];
  });
}

export function parseSessionState(body: unknown): SessionState {
  if (!body || typeof body !== "object") throw invalid("session");
  const record = body as Record<string, unknown>;
  if (typeof record.enabled !== "boolean") throw invalid("session.enabled");
  const providers = parseProviders(record.providers);
  // Off means cookies are ignored: whatever else the answer says, there is no session to use.
  if (!record.enabled) return { enabled: false, session: null, providers: [] };
  if (record.authenticated !== true) return { enabled: true, session: null, providers };
  const deviceId = text(record.device_id);
  const csrfToken = text(record.csrf_token);
  if (!deviceId || !csrfToken) throw invalid("session without device or CSRF token");
  return {
    enabled: true,
    providers,
    session: {
      deviceId,
      deviceLabel: text(record.device_label) ?? deviceId,
      authMethod: text(record.auth_method) ?? "device_pairing",
      expiresAt: text(record.expires_at),
      idleExpiresAt: text(record.idle_expires_at),
      csrfToken,
    },
  };
}

function parsePaired(body: unknown, now: number): ActiveSession {
  const record = (body && typeof body === "object" ? body : {}) as Record<string, unknown>;
  const deviceId = text(record.device_id);
  const csrfToken = text(record.csrf_token);
  if (!deviceId || !csrfToken) throw invalid("pair");
  const idle = typeof record.idle_timeout_seconds === "number" && record.idle_timeout_seconds > 0
    ? new Date(now + record.idle_timeout_seconds * 1000).toISOString() : undefined;
  return {
    deviceId,
    deviceLabel: text(record.device_label) ?? deviceId,
    authMethod: "device_pairing",
    expiresAt: text(record.expires_at),
    idleExpiresAt: idle,
    csrfToken,
  };
}

function parseDevices(body: unknown): Device[] {
  const devices = body && typeof body === "object" ? (body as Record<string, unknown>).devices : undefined;
  if (!Array.isArray(devices)) throw invalid("devices");
  return devices.flatMap((item): Device[] => {
    if (!item || typeof item !== "object") return [];
    const record = item as Record<string, unknown>;
    const deviceId = text(record.device_id);
    if (!deviceId) return [];
    return [{
      deviceId,
      deviceLabel: text(record.device_label) ?? deviceId,
      pairedAt: text(record.paired_at),
      lastSeenAt: text(record.last_seen_at),
      revokedAt: text(record.revoked_at),
      current: record.current === true,
    }];
  });
}

// ---- the client ---------------------------------------------------------------------------------------------

function identityRequest<T>(path: string, init: RequestInit, fetcher?: typeof fetch): Promise<T> {
  // A build of this page served from another host never sends a pairing code or a session call to that host.
  if (!isLocalPage()) return Promise.reject(new Error(`${IDENTITY_LOCAL_ONLY}: open the console on this computer (127.0.0.1)`));
  return requestJson<T>(`${IDENTITY_PATH}${path}`, init, fetcher ? { fetcher } : {});
}

/** GET /identity/v1/session: restores a paired session from the HttpOnly cookie, or reports none (or off). */
export async function getSession(fetcher?: typeof fetch): Promise<SessionState> {
  const body = await identityRequest<unknown>("/session",
    { method: "GET", ...SAME_ORIGIN_COOKIES, headers: { Accept: "application/json" } }, fetcher);
  return parseSessionState(body);
}

/** POST /identity/v1/pair with a code from `pados device pair`; the server sets the cookie and returns the CSRF token. */
export async function pair(code: string, label: string, fetcher?: typeof fetch, now: number = Date.now()): Promise<ActiveSession> {
  const pairingCode = normalizePairingCode(code);
  if (!PAIRING_CODE_PATTERN.test(pairingCode)) {
    throw new HttpError("SERVER_ERROR", 0, PAIRING_CODE_INCOMPLETE, "", false, "NO");
  }
  const deviceLabel = sanitizeDeviceLabel(label) || defaultDeviceLabel();
  const body = await identityRequest<unknown>("/pair", {
    method: "POST",
    ...SAME_ORIGIN_COOKIES,
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ pairing_code: pairingCode, device_label: deviceLabel }),
  }, fetcher);
  return parsePaired(body, now);
}

function sessionAuth(session: ActiveSession): SessionAuth {
  return { kind: "session", csrfToken: session.csrfToken, deviceId: session.deviceId };
}

/** POST /identity/v1/logout: revokes this browser's current session only (the device stays paired elsewhere). */
export async function logout(session: ActiveSession, fetcher?: typeof fetch): Promise<void> {
  await identityRequest<unknown>("/logout", authorizedInit(sessionAuth(session), { method: "POST", body: "{}" }), fetcher);
}

/** GET /identity/v1/devices, with this browser's session or with the control token. */
export async function listDevices(auth: ControlAuth, fetcher?: typeof fetch): Promise<Device[]> {
  return parseDevices(await identityRequest<unknown>("/devices", authorizedInit(auth, { method: "GET" }), fetcher));
}

const DEVICE_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

/** POST /identity/v1/devices/{device_id}/revoke: revokes the device and all of its sessions. */
export async function revokeDevice(auth: ControlAuth, deviceId: string, fetcher?: typeof fetch): Promise<void> {
  if (!DEVICE_ID_PATTERN.test(deviceId)) throw new HttpError("SERVER_ERROR", 0, "DEVICE_NOT_FOUND", "", false, "NO");
  await identityRequest<unknown>(`/devices/${encodeURIComponent(deviceId)}/revoke`,
    authorizedInit(auth, { method: "POST", body: "{}" }), fetcher);
}

/** The text a person confirms before a device is revoked. */
export function revokePrompt(device: Device): string {
  return [
    `Revoke device ${device.deviceLabel} (${device.deviceId})?`,
    device.current
      ? "This is the browser you are using: it is logged out now and must be paired again."
      : "Every session of that browser ends now; it must be paired again to make changes.",
  ].join("\n");
}

/** Revoke after the person confirms; nothing is sent when they decline. */
export async function confirmAndRevoke(auth: ControlAuth, device: Device, confirm: (message: string) => boolean,
                                       fetcher?: typeof fetch): Promise<"cancelled" | "revoked"> {
  if (!confirm(revokePrompt(device))) return "cancelled";
  await revokeDevice(auth, device.deviceId, fetcher);
  return "revoked";
}

// ---- what the page shows ------------------------------------------------------------------------------------

export type IdentityView =
  | { phase: "loading" }
  | { phase: "unavailable"; reason: string }
  | { phase: "disabled" }
  | { phase: "unpaired"; providers: IdentityProvider[] }
  | { phase: "paired"; session: ActiveSession; providers: IdentityProvider[] };

export function viewFromState(state: SessionState): IdentityView {
  if (!state.enabled) return { phase: "disabled" };
  return state.session
    ? { phase: "paired", session: state.session, providers: state.providers }
    : { phase: "unpaired", providers: state.providers };
}

/** Read the session (on load and on every reload of the page) and say what the page should offer. */
export async function loadIdentityView(fetcher?: typeof fetch): Promise<IdentityView> {
  if (!isLocalPage()) return { phase: "unavailable", reason: "Browser pairing works only from the console on this computer." };
  try {
    return viewFromState(await getSession(fetcher));
  } catch (error) {
    // An older server without /identity, or no server: the control token path stays exactly as it was.
    if (error instanceof HttpError && error.status === 404) {
      return { phase: "unavailable", reason: "This server does not offer browser pairing. Use the control token." };
    }
    return { phase: "unavailable", reason: `The browser session could not be checked (${identityFailure(error)}). Use the control token, or reload to try again.` };
  }
}

/** The notice to show when a session the page was using is gone after a fresh read. */
export function sessionEndedNotice(previous: IdentityView, next: IdentityView): string | null {
  if (previous.phase !== "paired" || next.phase === "paired") return null;
  if (next.phase === "disabled") return IDENTITY_MESSAGES.IDENTITY_DISABLED;
  return `This browser's session has ended (it expired or was revoked). Pair this browser again: run \`${PAIR_COMMAND}\` for a new code.`;
}

/** "Paired as <label> (device <id>), expires <time>" or "Not paired", and the other states in plain words. */
export function identityStatusText(view: IdentityView, formatTime: (value?: string) => string): string {
  switch (view.phase) {
    case "loading": return "Checking this browser's session…";
    case "unavailable": return "Not paired (pairing unavailable)";
    case "disabled": return "Not paired (browser pairing is off)";
    case "unpaired": return "Not paired";
    case "paired": {
      const { deviceLabel, deviceId, expiresAt } = view.session;
      return `Paired as ${deviceLabel} (device ${deviceId}), expires ${formatTime(expiresAt)}`;
    }
  }
}

export function controlAuthFor(view: IdentityView, controlValue: string): ControlAuth {
  return view.phase === "paired" ? sessionAuth(view.session) : controlValue;
}
