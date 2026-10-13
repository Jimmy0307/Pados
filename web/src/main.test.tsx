// SPDX-License-Identifier: AGPL-3.0-only
import { beforeEach, describe, expect, it, vi } from "vitest";
import { formatDate, queryString, shortId } from "./api";

describe("web API helpers", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("builds URL parameters without empty values", () => {
    expect(queryString({ q: "memory", page: 2, empty: "", missing: undefined })).toBe("?q=memory&page=2");
  });

  it("keeps identifiers recognizable but compact", () => {
    expect(shortId("12345678-1234-1234-1234-123456789abc")).toBe("12345678…789abc");
  });

  it("formats invalid dates as the source value", () => {
    expect(formatDate("not-a-date")).toBe("not-a-date");
    expect(formatDate()).toBe("—");
  });
});
