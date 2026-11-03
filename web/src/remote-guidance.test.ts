// SPDX-License-Identifier: AGPL-3.0-only
// OP-007: a remote notice tells the operator what happened and what is safe to do next, not only a code.
import { describe, expect, it } from "vitest";
import { RemoteApiError, describeRemoteFailure } from "./remote-api";

describe("remote failure guidance", () => {
  it("gives distinct, actionable guidance per outcome and keeps the request id", () => {
    const texts = (["REQUEST_TIMEOUT", "WRITE_OUTCOME_UNKNOWN", "SESSION_STALE", "NETWORK_ERROR"] as const)
      .map((outcome) => describeRemoteFailure(new RemoteApiError(0, "X", "req-1", outcome)));
    expect(new Set(texts).size).toBe(4);
    for (const text of texts) expect(text).toContain("Request req-1");
    expect(texts[1]).toMatch(/may or may not have been applied/);
    expect(texts[1]).toMatch(/Refresh/);  // check before trying again: never a blind retry of a write
    expect(texts[2]).toMatch(/[Ss]ign in again/);
  });

  it("names a server refusal by its code, with the request id", () => {
    const text = describeRemoteFailure(new RemoteApiError(409, "STEP_UP_BINDING_MISMATCH", "req-2"));
    expect(text).toContain("STEP_UP_BINDING_MISMATCH");
    expect(text).toContain("Request req-2");
  });

  it("never shows raw transport text for an unexpected error", () => {
    expect(describeRemoteFailure(new TypeError("Failed to fetch at http://internal:8000"))).toBe("The request failed.");
  });
});
