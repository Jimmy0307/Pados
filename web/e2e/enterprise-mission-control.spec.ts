import { test, expect, type Page } from "@playwright/test";

// Mocked projection: verifies only the minimal Enterprise visibility in Mission Control.
const projectId = "325f8168-efec-4818-99ac-b00a03d6accd";
const runId = "9b7c1a52-0000-4000-8000-00000000abcd";

function mission(enterprise: boolean) {
  return {
    projects: [{ project_id: projectId, project: { id: projectId, name: "Synthetic Enterprise Demo" }, project_status: "active",
      current_goal: null, blockers: [], next_decisions: [], active_runs: [], recent_accepted_results: [] }],
    system_health: { alive: true, configured: true, usable: true, ready: true, degraded: false, blocked: false, state: "ready", checks: [] },
    projection_only: true, authority: "PADOS_POSTGRESQL_AND_PROJECT_REGISTRY",
    ...(enterprise ? { enterprise: {
      status: "ENTERPRISE_EXTENSION_DEGRADED", core: "CORE_READY", projection_only: true, semantic_authority: "NONE",
      capabilities: [
        { capability_id: "knowledge.local", capability_type: "KNOWLEDGE_RETRIEVAL", version: "1", execution_mode: "IN_PROCESS",
          max_classification: "RESTRICTED", health: "READY", state: "ENTERPRISE_CAPABILITY_READY" },
        { capability_id: "rerank.remote", capability_type: "RERANK", version: "1", execution_mode: "REMOTE_HTTP",
          max_classification: "INTERNAL", health: "UNAUTHORIZED", state: "ENTERPRISE_CAPABILITY_UNAUTHORIZED", reason: "REMOTE_EGRESS_NOT_APPROVED" }],
      recent_evidence: [{ run_id: runId, project_id: projectId, provider_id: "PADOS_FILE_DIRECT_MAKER", execution_class: "LOCAL",
        package_hash: "a".repeat(64), source_ids: ["sha256:" + "1".repeat(64), "sha256:" + "2".repeat(64)],
        classifications: ["INTERNAL", "PUBLIC"], backends: [], reranker: {},
        authorization: { decision: "ALLOW", ceiling: "CONFIDENTIAL", withheld_count: 1 } }],
      authorization_failures: [{ kind: "EVIDENCE_WITHHELD", run_id: runId, count: 1 },
        { kind: "CAPABILITY_UNAUTHORIZED", capability_id: "rerank.remote", reason: "REMOTE_EGRESS_NOT_APPROVED" }],
    } } : {}),
  };
}

async function open(page: Page, enterprise: boolean) {
  await page.route("**/api/**", (route) => route.fulfill({ json: {} }));
  await page.route("**/api/projects", (route) => route.fulfill({ json: { projects: [] } }));
  await page.route("**/api/mission-control", (route) => route.fulfill({ json: mission(enterprise) }));
  await page.goto("/operator.html");
  await page.getByRole("button", { name: "Control Center" }).click();
  await expect(page.getByRole("heading", { name: "Mission Control" })).toBeVisible();
}

test("Mission Control shows enterprise health, provenance, provider, execution class and authorization failures", async ({ page }) => {
  await open(page, true);
  const card = page.getByRole("region", { name: "Enterprise capabilities" });
  await expect(card.getByRole("heading", { name: "Enterprise capabilities" })).toBeVisible();
  await expect(card.getByText("knowledge.local · KNOWLEDGE_RETRIEVAL · IN_PROCESS · max RESTRICTED")).toBeVisible();
  await expect(card.getByText("PADOS_FILE_DIRECT_MAKER · LOCAL · 2 sources (INTERNAL, PUBLIC)")).toBeVisible();
  await expect(card.getByText("EVIDENCE_WITHHELD")).toBeVisible();
  await expect(card.getByText("CAPABILITY_UNAUTHORIZED")).toBeVisible();
  await expect(card.getByText(/CORE READY/)).toBeVisible();
});

test("without the optional extension block Mission Control is unchanged", async ({ page }) => {
  await open(page, false);
  await expect(page.getByRole("heading", { name: "System health · READY" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Enterprise capabilities" })).toHaveCount(0);
});

test("live: enterprise block renders from the running PADOS API without changing Core health", async ({ page }) => {
  test.skip(process.env.PADOS_E2E_LIVE !== "ready", "requires a live PADOS API with PADOS_ENTERPRISE_CONFIG");
  const live = await (await page.request.get("/api/mission-control")).json();
  expect(live.enterprise, "optional enterprise block").toBeTruthy();
  await page.goto("/operator.html");
  await page.getByRole("button", { name: "Control Center" }).click();
  await expect(page.getByRole("heading", { name: `System health · ${live.system_health.state.toUpperCase()}` })).toBeVisible();
  const card = page.getByRole("region", { name: "Enterprise capabilities" });
  for (const capability of live.enterprise.capabilities) {
    await expect(card.getByText(new RegExp(`^${capability.capability_id} · `))).toBeVisible();
  }
  for (const evidence of live.enterprise.recent_evidence.slice(0, 1)) {
    await expect(card.getByText(new RegExp(`${evidence.provider_id} · ${evidence.execution_class}`)).first()).toBeVisible();
  }
});
