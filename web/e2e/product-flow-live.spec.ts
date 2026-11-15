import { test, expect, type Page, type Request } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// Unmocked: a real `pados serve` (identity sessions on), its PostgreSQL and the Vite proxy. Pairs this browser with a
// code from `pados device pair`, then drives the whole demo through the Control Center UI with the session only:
// 建立專案 → 新增 Markdown（含一個壞連結）→ 要求檢查 → 檢視計畫 → 核准 → 執行連結檢查 → 查看 Evidence（找出壞連結）
// → (the unfinished check waits for a human; cancel it, fix the link) → 再檢查 → 提交候選結果 → 等待使用者決策（WebAuthn）.
// scripts/dogfood/product_flow_e2e.py --playwright runs it and provides the environment; otherwise it is skipped.
const live = process.env.PADOS_E2E_PRODUCT_LIVE === "1";
const BROKEN = "# 使用指南\n\n請先閱讀[專案說明](README.md)。\n\n更多內容見[安裝步驟](missing-install.md)。\n";
const FIXED = "# 使用指南\n\n請先閱讀[專案說明](README.md)。\n";

function env(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required for the live product spec`);
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

async function runCheck(page: Page): Promise<void> {
  await page.getByRole("button", { name: "為這個專案提出工作需求" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "工作需求 → 執行" })).toBeVisible();
  await expect(page.getByLabel("要做什麼")).not.toBeEmpty();
  await page.getByRole("button", { name: "產生計畫", exact: true }).click();
  await expect(page.getByTestId("plan-source")).toContainText("確定性範本，未使用 AI");
}

test.describe("live product work loop", () => {
  test.skip(!live, "set PADOS_E2E_PRODUCT_LIVE=1 with a live pados serve (see scripts/dogfood/product_flow_e2e.py)");

  test("demo: create a project, find the broken link, fix it, submit the candidate and wait for the step-up", async ({ page }) => {
    test.setTimeout(600_000);
    const controlValue = readFileSync(env("PADOS_E2E_TOKEN_FILE"), "utf8").trim();
    const projectName = `文件檢查示範 ${Date.now()}`;
    const writes: Request[] = [];
    const leaks: string[] = [];
    page.on("request", (request) => { if (request.method() !== "GET" && /\/(control|identity)\//.test(request.url())) writes.push(request); });
    page.on("response", async (response) => {
      const text = await response.text().catch(() => "");
      if (text.includes(controlValue)) leaks.push(response.url());
    });

    // Pair this browser (the session is the only credential from here on).
    const code = mintPairingCode();
    await page.goto("/operator.html");
    await page.getByRole("button", { name: "Control Center" }).click();
    const nav = page.getByRole("navigation", { name: "Control center navigation" });
    await nav.getByRole("button", { name: "專案 Projects" }).click();
    await page.locator("input[name=pairing-code]").fill(code);
    await page.locator("input[name=device-label]").fill("Playwright product flow");
    await page.getByRole("button", { name: "Pair this browser", exact: true }).click();
    await expect(page.getByText("Paired as Playwright product flow (device dev_").first()).toBeVisible();

    // 建立專案: a name and a description, no path, no id.
    await expect(page.getByTestId("project-list")).toBeVisible();
    await page.getByLabel("名稱").fill(projectName);
    await page.getByLabel("說明（選填）").fill("產品流程示範：Markdown 連結檢查。");
    await page.getByRole("button", { name: "建立專案", exact: true }).click();
    await expect(page.getByTestId("project-name")).toHaveText(projectName);
    await expect(page.getByTestId("project-documents")).toContainText("README.md");
    await expect(page.getByTestId("project-history")).toContainText("尚未執行任何工作");
    await shot(page, "01-project-created");

    // 新增 Markdown with one good and one broken link, with the preview.
    await page.locator("input[name=document-path]").fill("guide.md");
    await page.locator("textarea[name=document-content]").fill(BROKEN);
    await expect(page.getByTestId("markdown-preview")).toContainText("missing-install.md");
    await shot(page, "02-markdown-with-broken-link");
    await page.getByRole("button", { name: "儲存 Markdown" }).click();
    await expect(page.getByText(/^已儲存 guide\.md/)).toBeVisible();
    await expect(page.getByTestId("project-documents").getByRole("button", { name: "編輯 guide.md" })).toBeVisible();

    // 要求檢查 → 檢視計畫 (deterministic, no AI) → 核准 → the TaskLoop runs.
    await runCheck(page);
    await expect(page.getByTestId("work-plan")).toContainText("markdown_reference_check");
    await expect(page.getByTestId("work-plan")).toContainText("LOW");
    await shot(page, "03-plan-review");
    await page.getByRole("button", { name: "核准", exact: true }).click();
    await expect(page.getByTestId("loop-status")).toContainText("WAITING_HUMAN", { timeout: 120_000 });

    // Evidence: the verified stderr report names the broken link; the unfinished loop cannot be submitted.
    await expect(page.getByTestId("broken-links")).toContainText("guide.md → missing-install.md");
    await expect(page.getByTestId("work-result")).toContainText("檢查發現問題");
    await expect(page.getByTestId("work-result")).toContainText("只有已完成的 TaskLoop 才能提交");
    await shot(page, "04-evidence-broken-link");
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await expect(page.getByTestId("loop-status")).toContainText("CANCELLED");

    // Fix the link in the document (replace with the SHA-256 that was read), then check again.
    await page.getByRole("button", { name: "開啟專案文件" }).click();
    await page.getByTestId("project-documents").getByRole("button", { name: "編輯 guide.md" }).click();
    await expect(page.locator("textarea[name=document-content]")).toHaveValue(BROKEN);
    await page.locator("textarea[name=document-content]").fill(FIXED);
    await page.getByRole("button", { name: "儲存 Markdown" }).click();
    await expect(page.getByText(/^已儲存 guide\.md/)).toBeVisible();
    await runCheck(page);
    await page.getByRole("button", { name: "核准", exact: true }).click();
    await expect(page.getByTestId("loop-status")).toContainText("COMPLETED", { timeout: 120_000 });
    await expect(page.getByTestId("work-evidence")).toContainText("所有相對連結都有效");
    await shot(page, "05-check-completed");

    // 提交候選結果 → waits for the user's decision with the Remote WebAuthn step-up; never shown as accepted.
    await page.getByRole("button", { name: "提交候選結果" }).click();
    const waiting = page.getByTestId("candidate-waiting");
    await expect(waiting).toContainText("等待您以 WebAuthn step-up 做出決策");
    await expect(waiting).toContainText("驗證 PASSED");
    await expect(waiting).toContainText("尚未設定 Remote 主控台");
    await expect(page.getByText("已透過 WebAuthn step-up 接受")).toHaveCount(0);
    await shot(page, "06-candidate-waiting-for-step-up");

    // The backend agrees: the candidate's approval is PENDING with no decision.
    const projects = await page.request.get(`${env("PADOS_E2E_API_URL")}/control/projects`, { headers: { "X-PADOS-Control-Token": controlValue } });
    const project = (await projects.json()).projects.find((p: { name: string }) => p.name === projectName);
    const work = await (await page.request.get(`${env("PADOS_E2E_API_URL")}/control/projects/${project.id}/work`,
      { headers: { "X-PADOS-Control-Token": controlValue } })).json();
    expect(work.candidates).toHaveLength(1);
    expect(work.candidates[0]).toMatchObject({ verification_status: "PASSED", approval_status: "PENDING", decision: null });
    expect(work.loops.map((loop: { status: string }) => loop.status).sort()).toEqual(["CANCELLED", "COMPLETED"]);

    // Phone layout for monitoring and approving.
    await page.setViewportSize({ width: 390, height: 844 });
    await shot(page, "07-phone-candidate-waiting");

    // English fallback switch still shows the same state.
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.getByRole("button", { name: "English", exact: true }).click();
    await expect(page.getByTestId("candidate-waiting")).toContainText("Waiting for your decision with WebAuthn step-up");

    expect(writes.length).toBeGreaterThan(10);
    expect(writes.every((request) => request.headers()["x-pados-control-token"] === undefined)).toBe(true);
    expect(writes.filter((r) => /\/control\//.test(r.url())).every((request) => Boolean(request.headers()["x-pados-csrf"]))).toBe(true);
    const memory = await page.evaluate(() => [window.localStorage.length, window.sessionStorage.length, document.documentElement.outerHTML]);
    expect(memory[0]).toBe(0);
    expect(memory[1]).toBe(0);
    expect(String(memory[2])).not.toContain(controlValue);
    expect(leaks).toEqual([]);
  });
});
