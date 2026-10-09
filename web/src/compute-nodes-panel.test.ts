// SPDX-License-Identifier: AGPL-3.0-only
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ComputeNodesVisibility } from "./api";
import { ComputeNodesPanel } from "./compute-nodes-panel";

const computeNodes: ComputeNodesVisibility = {
  status: "COMPUTE_NODES_CONFIGURED", projection_only: true,
  nodes: [
    { node_id: "lcn-0123456789abcdef0123", node_name: "windows-dev-node", state: "ONLINE", stale: false, authorized: true,
      platform: "WINDOWS", architecture: "amd64", capabilities: ["LOCAL_INFERENCE"],
      models: [{ model_id: "ollama/local-model@abc123def456", availability: "AVAILABLE" }],
      current_load: { running: 0, queued: 0, max_concurrent_jobs: 1, max_queue_depth: 2 },
      last_successful_check: "2026-09-30T00:00:00+00:00",
      last_execution: { run_id: "12345678-1234-1234-1234-123456789abc", model_id: "m", status: "SUCCEEDED", finished_at: "x" },
      execution_class: "LOCAL" },
    { node_id: "lcn-ffffffffffffffffffff", node_name: "mac-node", state: "OFFLINE", stale: true, authorized: false,
      capabilities: [], models: [], execution_class: "LOCAL", reason: "NODE_OFFLINE" },
  ],
};

describe("Compute nodes panel", () => {
  it("renders nothing without registered nodes", () => {
    expect(renderToStaticMarkup(createElement(ComputeNodesPanel, {}))).toBe("");
  });

  it("shows state, platform, capabilities, models, load, last check, last execution and reasons", () => {
    const html = renderToStaticMarkup(createElement(ComputeNodesPanel, { computeNodes }));
    expect(html).toContain("windows-dev-node");
    expect(html).toContain("WINDOWS amd64 · LOCAL · LOCAL_INFERENCE");
    expect(html).toContain("ollama/local-model@abc123def456 (AVAILABLE)");
    expect(html).toContain("load 0/1 running, 0 queued");
    expect(html).toContain("last execution run 12345678…789abc · SUCCEEDED");
    expect(html).toContain("OFFLINE (STALE)");
    expect(html).toContain("NODE_OFFLINE");
  });
});
