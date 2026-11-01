// SPDX-License-Identifier: AGPL-3.0-only
// OP-007: request deadlines, the server error envelope, distinct timeout / write-unknown / stale-session outcomes,
// safe-read-only retry and duplicate-action protection.
import { afterEach, describe, expect, it, vi } from "vitest";
import { DEADLINE_HEADER, describeFailure, HttpError, REQUEST_ID_HEADER, requestJson, singleFlight } from "./http";
import { getJson } from "./api";
import { RemoteApiError, RemoteClient } from "./remote-api";

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const hanging = (_input: RequestInfo | URL, init?: RequestInit): Promise<Response> =>
  new Promise((_resolve, reject) => {
    init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
  });

/** Resolve to the rejection of a promise that must fail, typed for assertions. */
const rejection = <E = HttpError>(promise: Promise<unknown>): Promise<E> =>
  promise.then(() => { throw new Error("expected the request to fail"); }, (error: unknown) => error as E);

const envelope = (status: number, code: string, extra: Record<string, unknown> = {}) =>
  new Response(JSON.stringify({
    detail: code,
    error: { schema_version: "pados.error.v1", code, request_id: "srv-req-1", retryable: false, write_occurred: "NO", ...extra },
  }), { status, headers: { "Content-Type": "application/json" } });

describe("request deadline", () => {
  it("aborts a read that exceeds its deadline, retries it once, then reports REQUEST_TIMEOUT", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(hanging);
    const pending = rejection(requestJson("/control/approvals", {}, { timeoutMs: 50, fetcher }));
    await vi.advanceTimersByTimeAsync(200);
    const error = await pending;
    expect(error).toBeInstanceOf(HttpError);
    expect([error.outcome, error.writeOccurred]).toEqual(["REQUEST_TIMEOUT", "NO"]);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(describeFailure(error)).toContain("did not answer in time");
  });

  it("never retries a write that timed out and reports its outcome as unknown", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn(hanging);
    const pending = rejection(
      requestJson("/control/work-requests", { method: "POST", body: "{}" }, { timeoutMs: 50, fetcher }));
    await vi.advanceTimersByTimeAsync(200);
    const error = await pending;
    expect([error.outcome, error.writeOccurred, error.retryable]).toEqual(["WRITE_OUTCOME_UNKNOWN", "UNKNOWN", false]);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(describeFailure(error)).toContain("may or may not have been applied");
  });

  it("sends a request id and the deadline to the server", async () => {
    const fetcher = vi.fn(async () => new Response("{}", { status: 200 }));
    await requestJson("/health/ready", {}, { timeoutMs: 1234, fetcher });
    const headers = (fetcher.mock.calls[0] as unknown[])[1] as RequestInit;
    const sent = headers.headers as Record<string, string>;
    expect(sent[REQUEST_ID_HEADER]).toMatch(/^web-[0-9a-f]{24}$/);
    expect(sent[DEADLINE_HEADER]).toBe("1234");
  });
});

describe("server error envelope", () => {
  it("keeps the server code, request id, retryability and write outcome", async () => {
    const fetcher = vi.fn(async () => envelope(409, "WORK_REQUEST_IDEMPOTENCY_CONTENT_CONFLICT", { write_occurred: "NO" }));
    const error = await rejection(
      requestJson("/control/work-requests", { method: "POST", body: "{}" }, { fetcher }));
    expect([error.outcome, error.status, error.code, error.requestId, error.writeOccurred]).toEqual(
      ["SERVER_ERROR", 409, "WORK_REQUEST_IDEMPOTENCY_CONTENT_CONFLICT", "srv-req-1", "NO"]);
  });

  it("retries a safe read only when the server says it is retryable", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(envelope(503, "DATABASE_UNAVAILABLE", { retryable: true }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    await expect(requestJson("/control/approvals", {}, { fetcher })).resolves.toEqual({ ok: true });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it("reports an expired session as SESSION_STALE", async () => {
    const fetcher = vi.fn(async () => envelope(401, "REMOTE_AUTH_INVALID"));
    const error = await rejection(requestJson("/remote/v1/attention", {}, { fetcher, retrySafeReads: false }));
    expect(error.outcome).toBe("SESSION_STALE");
    expect(describeFailure(error)).toContain("Sign in again");
  });

  it("surfaces the request id on operator API errors", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(envelope(404, "NOT_FOUND"));
    const error = await rejection<{ code: string; request_id: string; outcome: string }>(getJson("/control/approvals/x"));
    expect([error.code, error.request_id, error.outcome]).toEqual(["NOT_FOUND", "srv-req-1", "SERVER_ERROR"]);
  });

  it("gives operators the timeout guidance, not the raw browser exception", async () => {
    vi.useFakeTimers();
    vi.spyOn(globalThis, "fetch").mockImplementation(hanging);
    const pending = rejection<{ code: string; message: string; outcome: string; request_id: string }>(
      getJson("/control/approvals"));
    await vi.advanceTimersByTimeAsync(40_000);
    const error = await pending;
    expect([error.code, error.outcome]).toEqual(["REQUEST_TIMEOUT", "REQUEST_TIMEOUT"]);
    expect(error.message).toContain("did not answer in time");
    expect(error.message).not.toContain("AbortError");
    expect(error.request_id).toMatch(/^web-/);
  });

  it("keeps RemoteApiError for remote callers, with the request id", async () => {
    const fetcher = vi.fn(async () => envelope(403, "REMOTE_APPROVAL_NOT_ALLOWED"));
    const client = new RemoteClient({ actor: "owner", proof: "p" }, fetcher as unknown as typeof fetch);
    const error = await rejection<RemoteApiError>(client.attention());
    expect(error).toBeInstanceOf(RemoteApiError);
    expect([error.status, error.detail, error.requestId]).toEqual([403, "REMOTE_APPROVAL_NOT_ALLOWED", "srv-req-1"]);
  });
});

describe("duplicate action protection", () => {
  it("sends an identical write only once while it is in flight", async () => {
    let resolve: (value: Response) => void = () => {};
    const fetcher = vi.fn(() => new Promise<Response>((r) => { resolve = r; }));
    const send = () => singleFlight("POST /control/loops/l/step {\"k\":1}", () =>
      requestJson("/control/loops/l/step", { method: "POST", body: "{\"k\":1}" }, { fetcher }));
    const first = send();
    const second = send();
    expect(second).toBe(first);
    resolve(new Response(JSON.stringify({ status: "ok" }), { status: 200 }));
    await expect(first).resolves.toEqual({ status: "ok" });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
