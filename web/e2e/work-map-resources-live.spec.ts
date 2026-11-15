import { test, expect, request as apiRequest, type Page, type Request } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// Unmocked: a real `pados serve` (identity sessions on), its PostgreSQL and the Vite proxy. The demo project was created
// through the product work loop (scripts/dogfood/work_map_resources_e2e.py runs product_flow_e2e's flow first: broken link
// → WAITING_HUMAN → cancelled → fixed → COMPLETED → ResultCandidate waiting for the WebAuthn step-up). This spec pairs the
// browser, finds that task in 工作心智圖 with its Evidence and decision state, switches mind map ↔ list, checks that the
// view state survives a page switch, opens a work request from a goal (not submitted), and reads 資源與儲存. Neither page
// writes anything: the only non-GET calls are the pairing ones under /identity.
const live = process.env.PADOS_E2E_MAP_LIVE === "1";

function env(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required for the live work map spec`);
  return value;
}

function mintPairingCode(): string {
  const output = execFileSync(env("PADOS_E2E_PYTHON"), ["-m", "pados.cli.main", "device", "pair", "--json", "--base-url",
    env("PADOS_E2E_API_URL"), "--token-file", env("PADOS_E2E_TOKEN_FILE")], { encoding: "utf8", env: { ...process.env, PYTHONUTF8: "1" }, timeout: 60_000 });
  return String(JSON.parse(output.slice(output.indexOf("{"), output.lastIndexOf("}") + 1)).pairing_code);
}

async function shot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: join(env("PADOS_E2E_EVIDENCE_DIR"), `${name}.png`), fullPage: true });
}

async function openPage(page: Page, name: string): Promise<void> {
  await page.getByRole("navigation", { name: "Control center navigation" }).getByRole("button", { name }).click();
}

/** Pair through the UI on the Work Map page (same bounded retry as the product spec for a cold Vite proxy). */
async function pairBrowser(page: Page, label: string): Promise<void> {
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    if (attempt > 1) {
      await page.reload();
      await page.getByRole("button", { name: "Control Center" }).click();
    }
    await openPage(page, "工作心智圖 (Work Map)");
    await page.waitForLoadState("networkidle");
    if (await page.getByText(`Paired as ${label} (device dev_`).first().isVisible()) return;
    await page.locator("input[name=pairing-code]").fill(mintPairingCode());
    await page.locator("input[name=device-label]").fill(label);
    await page.getByRole("button", { name: "Pair this browser", exact: true }).click();
    try {
      await expect(page.getByText(`Paired as ${label} (device dev_`).first()).toBeVisible({ timeout: 20_000 });
      return;
    } catch (error) {
      if (attempt === 3) throw error;
    }
  }
}

test.describe("live work map and resources", () => {
  test.skip(!live, "set PADOS_E2E_MAP_LIVE=1 with a live pados serve (see scripts/dogfood/work_map_resources_e2e.py)");

  test("the demo task is findable in the Work Map with its evidence and decision; Resources measure or say UNKNOWN", async ({ page }) => {
    test.setTimeout(300_000);
    const projectName = env("PADOS_E2E_PROJECT_NAME");
    const controlValue = readFileSync(env("PADOS_E2E_TOKEN_FILE"), "utf8").trim();
    const writes: Request[] = [];
    page.on("request", (request) => { if (request.method() !== "GET" && /\/control\//.test(request.url())) writes.push(request); });

    await page.goto("/operator.html");
    await page.getByRole("button", { name: "Control Center" }).click();
    await pairBrowser(page, "Playwright work map");

    // 工作心智圖: choose the demo project; the mind map is drawn from the backend projection.
    await expect(page.getByRole("heading", { level: 1, name: "工作心智圖" })).toBeVisible();
    await page.locator("select[name=map-project]").selectOption({ label: projectName });
    const canvas = page.getByTestId("work-map-canvas");
    await expect(canvas).toBeVisible();
    await expect(canvas.locator("[data-kind=goal]")).toHaveCount(2);
    await expect(canvas.locator("svg.map-edges path").first()).toBeAttached();
    await shot(page, "01-work-map-mindmap");

    // Find the second (fixed) check by its words: the goal is selected, its run COMPLETED, waiting for the step-up decision.
    await page.locator("input[name=map-search]").fill("重新檢查文件連結");
    const detail = page.getByTestId("work-map-detail");
    await expect(detail.getByTestId("map-detail-title")).toHaveText("重新檢查文件連結");
    await expect(detail.getByTestId("map-detail-status")).toContainText("COMPLETED");
    await expect(detail.getByTestId("map-detail-blocking")).toContainText("尚未設定 Remote 主控台");
    await expect(detail.getByTestId("map-detail-history")).toContainText("COMPLETED");

    // Expand everything: the run's Evidence (job artifacts with SHA-256) and the ResultCandidate decision are in the map.
    await page.getByRole("button", { name: "全部展開" }).click();
    const evidence = canvas.locator("[data-kind=evidence]");
    expect(await evidence.count()).toBeGreaterThanOrEqual(2);
    await expect(canvas.locator("[data-kind=evidence]", { hasText: "stderr" }).first()).toBeVisible();
    await canvas.locator("[data-kind=evidence]", { hasText: "stderr" }).first().locator(".map-node-main").click();
    await expect(detail).toContainText("SHA-256");
    await expect(detail.locator("code.map-fact").first()).toHaveText(/^[0-9a-f]{64}$/);
    await shot(page, "02-work-map-evidence");
    const decision = canvas.locator("[data-node-id^='decision:candidate:']");
    await expect(decision).toHaveCount(1);
    await decision.locator(".map-node-main").click();
    await expect(detail.getByTestId("map-detail-status")).toContainText("WAITING_STEP_UP");
    await expect(detail).toContainText("PENDING");
    await expect(canvas.getByText("ACCEPTED", { exact: true })).toHaveCount(0);
    await shot(page, "03-work-map-decision-waiting");

    // The first check: its run was cancelled after the check waited for a human (WAIT_FOR_HUMAN).
    const cancelled = canvas.locator("[data-kind=execution][data-node-id^='execution:loop:']", { hasText: "TaskLoop" }).filter({ hasText: "CANCELLED" });
    await expect(cancelled).toHaveCount(1);
    await expect(canvas.locator("[data-kind=execution]", { hasText: "WAIT_FOR_HUMAN" }).first()).toBeVisible();

    // List view shows the same tree; the view state survives a page switch (page memory, data re-read).
    await page.getByRole("button", { name: "清單", exact: true }).click();
    const list = page.getByTestId("work-map-list");
    await expect(list).toBeVisible();
    await expect(list.getByRole("treeitem", { selected: true })).toContainText("ResultCandidate");
    await shot(page, "04-work-map-list");
    await openPage(page, "資源與儲存 (Resources)");
    await openPage(page, "工作心智圖 (Work Map)");
    await expect(page.getByTestId("work-map-list")).toBeVisible();
    await expect(page.getByRole("button", { name: "清單", exact: true })).toHaveAttribute("aria-pressed", "true");
    await expect(page.getByTestId("work-map-detail").getByTestId("map-detail-status")).toContainText("WAITING_STEP_UP");

    // A new work request from a goal opens the existing governed flow with the goal's words (not submitted here).
    await list.locator("[data-node-id^='goal:']", { hasText: "重新檢查文件連結" }).locator(".map-row-main").click();
    await page.getByRole("button", { name: "從這裡提出工作需求" }).click();
    await expect(page.getByRole("heading", { level: 1, name: "工作需求 → 執行" })).toBeVisible();
    await expect(page.locator("input[name=title]")).toHaveValue("重新檢查文件連結");
    await expect(page.locator("select[name=project] option:checked")).toHaveText(projectName);
    await shot(page, "05-work-request-from-goal");

    // 資源與儲存: measured values with their source; UNKNOWN where nothing was measured; cleanup is only a preview.
    await openPage(page, "資源與儲存 (Resources)");
    await expect(page.getByRole("heading", { level: 1, name: "資源與儲存" })).toBeVisible();
    for (const id of ["ram_in_use", "pados_process_rss", "database_size", "database_index_size", "execution_records", "evidence_files", "project_documents"]) {
      await expect(page.getByTestId(`metric-${id}`)).toHaveAttribute("data-status", "MEASURED");
    }
    const model = page.getByTestId("metric-model_resident_memory");
    await expect(model).toHaveAttribute("data-status", "UNKNOWN");
    await expect(model).toContainText("MODEL_RUNTIME_NOT_CONFIGURED");
    await expect(page.getByTestId("metric-token_usage")).toHaveAttribute("data-status", "UNKNOWN");
    await expect(page.getByTestId("metric-database_size")).toContainText("PostgreSQL pg_database_size");
    await expect(page.getByTestId("resources-budget")).toContainText("可用低於 15% 時警告");
    await expect(page.getByTestId("trend-storage-unknown")).toContainText("HISTORY_NOT_RECORDED");
    await shot(page, "06-resources");
    const cleanup = page.getByTestId("resources-cleanup");
    await expect(cleanup).toContainText("需要 Owner 授權");
    await expect(cleanup.getByTestId("cleanup-protected")).toContainText("job_artifacts");
    await expect(cleanup.getByRole("button")).toHaveCount(0);
    await cleanup.scrollIntoViewIfNeeded();
    await shot(page, "07-resources-cleanup-preview");

    // Phone layout.
    await page.setViewportSize({ width: 390, height: 844 });
    await shot(page, "08-phone-resources");
    await openPage(page, "工作心智圖 (Work Map)");
    await expect(page.getByTestId("work-map-list")).toBeVisible();
    await shot(page, "09-phone-work-map-list");

    // The backend agrees, through an independent caller with the operator's control token.
    const operator = await apiRequest.newContext({ baseURL: env("PADOS_E2E_API_URL"), extraHTTPHeaders: { "X-PADOS-Control-Token": controlValue } });
    const projects = await (await operator.get("/control/projects", { timeout: 60_000 })).json();
    const project = projects.projects.find((p: { name: string }) => p.name === projectName);
    const map = await (await operator.get(`/control/projects/${project.id}/work-map`, { timeout: 60_000 })).json();
    await operator.dispose();
    expect(map.authority).toBe("PROJECTION_ONLY");
    expect(map.candidates[0]).toMatchObject({ approval_status: "PENDING", decision: null });

    expect(writes.map((r) => `${r.method()} ${new URL(r.url()).pathname}`)).toEqual([]);
    const memory = await page.evaluate(() => [window.localStorage.length, window.sessionStorage.length]);
    expect(memory).toEqual([0, 0]);
  });
});
