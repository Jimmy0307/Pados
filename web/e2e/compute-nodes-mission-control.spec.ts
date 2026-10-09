import { test, expect, type Page } from "@playwright/test";

// Mocked projection: verifies only the minimal compute-node view in Mission Control.
const base = {
  projects: [], projection_only: true, authority: "PADOS_POSTGRESQL_AND_PROJECT_REGISTRY",
  system_health: { alive: true, configured: true, usable: true, ready: true, degraded: false, blocked: false, state: "ready", checks: [] },
};
const computeNodes = {
  status: "COMPUTE_NODES_CONFIGURED", projection_only: true,
  nodes: [{ node_id: "lcn-0123456789abcdef0123", node_name: "windows-dev-node", state: "ONLINE", stale: false, authorized: true,
    platform: "WINDOWS", architecture: "amd64", capabilities: ["LOCAL_INFERENCE"],
    models: [{ model_id: "ollama/local-model@abc123def456", availability: "AVAILABLE" }],
    current_load: { running: 1, queued: 0, max_concurrent_jobs: 1, max_queue_depth: 2 },
    last_successful_check: "2026-09-30T00:00:00+00:00",
    last_execution: { run_id: "9b7c1a52-0000-4000-8000-00000000abcd", model_id: "m", status: "SUCCEEDED", finished_at: "x" },
    execution_class: "LOCAL" },
  { node_id: "lcn-ffffffffffffffffffff", node_name: "offline-node", state: "OFFLINE", stale: false, authorized: true,
    capabilities: [], models: [], execution_class: "LOCAL", reason: "NODE_OFFLINE" }],
};

async function open(page: Page, withNodes: boolean) {
  await page.route("**/api/**", (route) => route.fulfill({ json: {} }));
  await page.route("**/api/projects", (route) => route.fulfill({ json: { projects: [] } }));
  await page.route("**/api/mission-control", (route) => route.fulfill({ json: withNodes ? { ...base, compute_nodes: computeNodes } : base }));
  await page.goto("/operator.html");
  await page.getByRole("button", { name: "Control Center" }).click();
  await expect(page.getByRole("heading", { name: "Mission Control" })).toBeVisible();
}

test("Mission Control shows registered nodes, state, platform, models, load, last check/execution and reasons", async ({ page }) => {
  await open(page, true);
  const card = page.getByRole("region", { name: "Local compute nodes" });
  await expect(card.getByText("windows-dev-node")).toBeVisible();
  await expect(card.getByText("WINDOWS amd64 · LOCAL · LOCAL_INFERENCE")).toBeVisible();
  await expect(card.getByText("models: ollama/local-model@abc123def456 (AVAILABLE)")).toBeVisible();
  await expect(card.getByText("load 1/1 running, 0 queued")).toBeVisible();
  await expect(card.getByText(/last execution run 9b7c1a52/)).toBeVisible();
  await expect(card.getByText("OFFLINE", { exact: true })).toBeVisible();
  await expect(card.getByText("NODE_OFFLINE")).toBeVisible();
});

test("without registered nodes Mission Control is unchanged", async ({ page }) => {
  await open(page, false);
  await expect(page.getByRole("heading", { name: "System health · READY" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Local compute nodes" })).toHaveCount(0);
});

test("live: registered node renders from the running PADOS API without changing Core health", async ({ page }) => {
  test.skip(process.env.PADOS_E2E_LIVE !== "ready", "requires a live PADOS API with PADOS_COMPUTE_NODES_CONFIG");
  const live = await (await page.request.get("/api/mission-control")).json();
  expect(live.compute_nodes?.nodes?.length, "registered compute nodes").toBeGreaterThan(0);
  await page.goto("/operator.html");
  await page.getByRole("button", { name: "Control Center" }).click();
  await expect(page.getByRole("heading", { name: `System health · ${live.system_health.state.toUpperCase()}` })).toBeVisible();
  const card = page.getByRole("region", { name: "Local compute nodes" });
  for (const node of live.compute_nodes.nodes) {
    await expect(card.getByText(node.node_name || node.node_id, { exact: true })).toBeVisible();
  }
});
