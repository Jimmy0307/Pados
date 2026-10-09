// SPDX-License-Identifier: AGPL-3.0-only
import { afterEach, describe, expect, it, vi } from "vitest";
import { authorizeBatch, createBatch, getBatch } from "./api";

afterEach(() => vi.restoreAllMocks());

describe("operator API client", () => {
  it("uploads files as multipart without exposing local paths", async () => {
    const response = { batch: { batch_id: "batch" }, manifest_sha256: "a".repeat(64) };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(response), { status: 201 }));
    const file = new File(["pdf"], "paper.pdf", { type: "application/pdf" });
    const result = await createBatch("project/id", [file]);
    expect(result).toEqual({ ...response, batch: { ...response.batch, manifest_sha256: response.manifest_sha256 } });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/projects/project%2Fid/corpus/batches");
    expect(init?.method).toBe("POST");
    expect(init?.body).toBeInstanceOf(FormData);
    expect((init?.body as FormData).get("files")).toBeInstanceOf(File);
  });

  it("sends the server manifest and selected hashes for human authorization", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify({ status: "ok" }), { status: 200 }));
    await authorizeBatch({ batch_id: "batch", manifest_sha256: "a".repeat(64), selected_file_hashes: { "paper.pdf": "b".repeat(64) }, project_id: "project", status: "PENDING_HUMAN_AUTHORIZATION" });
    const init = fetchMock.mock.calls[0][1];
    expect(JSON.parse(String(init?.body))).toEqual({ manifest_sha256: "a".repeat(64), selected_file_hashes: { "paper.pdf": "b".repeat(64) } });
  });

  it("binds envelope manifest metadata to the batch returned by getBatch", async () => {
    const response = {
      status: "ok",
      batch: { batch_id: "batch", project_id: "project", status: "PENDING_HUMAN_AUTHORIZATION" },
      manifest_sha256: "a".repeat(64),
    };
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(JSON.stringify(response), { status: 200 }));

    const result = await getBatch("batch");

    expect(result.batch.manifest_sha256).toBe("a".repeat(64));
  });
});
