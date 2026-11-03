// SPDX-License-Identifier: AGPL-3.0-only
// OP-007: one request helper for the operator and remote clients. Every request carries a request id, and the
// server's pados.error.v1 envelope is preserved. A safe read has a deadline (AbortController, covering the body too)
// that is also sent to the server as its budget. A write has no client deadline by default: aborting it never stops the
// server, it would only turn a known outcome into an unknown one, and its budget must not cap long server work (model
// calls, ingestion). Outcomes stay distinct: a timed-out read (REQUEST_TIMEOUT), a write whose outcome is unknown
// (WRITE_OUTCOME_UNKNOWN, never retried), a stale session (SESSION_STALE) and a network failure. Only safe reads are
// retried, once. An identical write already in flight is not sent twice.

export const REQUEST_ID_HEADER = "X-Request-ID";
export const DEADLINE_HEADER = "X-Request-Deadline-Ms";
export const DEFAULT_TIMEOUT_MS = 15_000;

export type WriteOccurred = "YES" | "NO" | "UNKNOWN";
export type RequestOutcome = "SERVER_ERROR" | "REQUEST_TIMEOUT" | "WRITE_OUTCOME_UNKNOWN" | "SESSION_STALE" | "NETWORK_ERROR";

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const STALE_SESSION_CODES = new Set([
  "REMOTE_AUTH_INVALID",
  "REMOTE_AUTH_REQUIRED",
  "SESSION_UNAUTHENTICATED",
  "APPROVAL_STEP_UP_SESSION_REQUIRED",
  "UNAUTHENTICATED",
]);

export class HttpError extends Error {
  constructor(
    readonly outcome: RequestOutcome,
    readonly status: number,
    readonly code: string,
    readonly requestId: string,
    readonly retryable: boolean,
    readonly writeOccurred: WriteOccurred,
    readonly detail: unknown = undefined,
  ) {
    super(`${code} (${status || "no response"}, request ${requestId})`);
  }
}

export function newRequestId(): string {
  const bytes = new Uint8Array(12);
  globalThis.crypto.getRandomValues(bytes);
  return `web-${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

export type RequestOptions = {
  /** Client deadline in ms; ``null`` for none. Default: DEFAULT_TIMEOUT_MS for safe reads, none for writes. */
  timeoutMs?: number | null;
  fetcher?: typeof fetch;
  retrySafeReads?: boolean;
};

type Envelope = { code?: string; request_id?: string; retryable?: boolean; write_occurred?: WriteOccurred };

function isSafe(init: RequestInit): boolean {
  return SAFE_METHODS.has((init.method || "GET").toUpperCase());
}

type Received = { response: Response; body: { detail?: unknown; error?: Envelope }; requestId: string };

async function once(url: string, init: RequestInit, timeoutMs: number | null, fetcher: typeof fetch): Promise<Received> {
  const controller = new AbortController();
  const timer = timeoutMs === null ? undefined : setTimeout(() => controller.abort(), timeoutMs);
  const requestId = newRequestId();
  const budget: Record<string, string> = timeoutMs === null ? {} : { [DEADLINE_HEADER]: String(timeoutMs) };
  try {
    const response = await fetcher(url, {
      ...init,
      signal: controller.signal,
      headers: { [REQUEST_ID_HEADER]: requestId, ...budget, ...(init.headers || {}) },
    });
    // The deadline covers the body as well: a stalled body is aborted like a stalled response.
    const body = (await response.json().catch((error: unknown) => {
      if (controller.signal.aborted) throw error;
      return {};
    })) as Received["body"];
    return { response, body, requestId };
  } catch {
    const safe = isSafe(init);
    const aborted = controller.signal.aborted;
    // A write that timed out or lost the network may have been applied: its outcome is unknown, never "failed".
    // The raw browser exception is not carried: operators get the outcome and describeFailure's guidance instead.
    const outcome: RequestOutcome = !safe ? "WRITE_OUTCOME_UNKNOWN" : aborted ? "REQUEST_TIMEOUT" : "NETWORK_ERROR";
    throw new HttpError(outcome, 0, outcome, requestId, safe, safe ? "NO" : "UNKNOWN");
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

/** Fetch JSON with a deadline; throws HttpError with the server envelope or a distinct client-side outcome. */
export async function requestJson<T>(url: string, init: RequestInit = {}, options: RequestOptions = {}): Promise<T> {
  const timeoutMs = options.timeoutMs !== undefined ? options.timeoutMs : isSafe(init) ? DEFAULT_TIMEOUT_MS : null;
  const fetcher = options.fetcher ?? ((input, requestInit) => fetch(input, requestInit));
  const attempts = isSafe(init) && options.retrySafeReads !== false ? 2 : 1;
  let lastError: unknown;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    let received: Received;
    try {
      received = await once(url, init, timeoutMs, fetcher);
    } catch (error) {
      lastError = error;
      if (error instanceof HttpError && error.retryable && attempt < attempts) continue;
      throw error;
    }
    const { response, body } = received;
    if (response.ok) return body as T;
    const envelope = body.error || {};
    const requestId = envelope.request_id || response.headers.get(REQUEST_ID_HEADER) || received.requestId;
    const code = envelope.code || (typeof body.detail === "string" ? body.detail : `HTTP_${response.status}`);
    const stale = (response.status === 401 || response.status === 403) && STALE_SESSION_CODES.has(code);
    const error = new HttpError(stale ? "SESSION_STALE" : "SERVER_ERROR", response.status, code, requestId,
      Boolean(envelope.retryable), envelope.write_occurred || (isSafe(init) ? "NO" : "UNKNOWN"), body.detail);
    if (error.retryable && isSafe(init) && attempt < attempts) {
      lastError = error;
      continue;
    }
    throw error;
  }
  throw lastError;
}

export type SingleFlight = <T>(key: string, action: () => Promise<T>) => Promise<T>;

/**
 * Duplicate-action protection with its own scope: an identical write already in flight in this scope returns the same
 * promise, not a second send. Each client (each sign-in or session) needs its own scope, so two clients never share
 * one another's request, challenge or response. The key is method, URL and body only; credentials never enter it.
 */
export function createSingleFlight(): SingleFlight {
  const inFlight = new Map<string, Promise<unknown>>();
  return <T>(key: string, action: () => Promise<T>): Promise<T> => {
    const existing = inFlight.get(key) as Promise<T> | undefined;
    if (existing) return existing;
    const promise = action().finally(() => inFlight.delete(key));
    inFlight.set(key, promise);
    return promise;
  };
}

/** The local console's scope: one page, one operator session. Remote clients each create their own. */
export const singleFlight: SingleFlight = createSingleFlight();

export function actionKey(method: string, url: string, body: BodyInit | null | undefined): string {
  return `${method.toUpperCase()} ${url} ${typeof body === "string" ? body : body ? "[binary]" : ""}`;
}

/** A short operator-facing description of a failed request, distinct per outcome. */
export function describeFailure(error: unknown): string {
  if (!(error instanceof HttpError)) return error instanceof Error ? error.message : String(error);
  const reference = error.requestId ? ` Request ${error.requestId}.` : "";
  switch (error.outcome) {
    case "REQUEST_TIMEOUT":
      return `The server did not answer in time.${reference}`;
    case "WRITE_OUTCOME_UNKNOWN":
      return `The change may or may not have been applied. Refresh and check before trying again.${reference}`;
    case "SESSION_STALE":
      return `Your session is no longer valid. Sign in again.${reference}`;
    case "NETWORK_ERROR":
      return `The server could not be reached.${reference}`;
    default:
      return `${error.code}.${reference}`;
  }
}
