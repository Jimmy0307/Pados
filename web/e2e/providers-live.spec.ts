import { test, expect, request as apiRequest, type Locator, type Page } from "@playwright/test";
import { join } from "node:path";

// Unmocked: a real `pados serve` from a temporary runtime, the simulated enterprise gateway as its own process and the
// Vite proxy. scripts/dogfood/gateway_simulated_e2e.py --playwright provides the environment; otherwise skipped.
// AI 模型與連線 (Providers): 輸入 control token → 測試連線（已連線，模型未授權）→ 以探測訊息測試（非權威觀察結果）→ 閘道故障
// （無法連線，白話原因，可展開代碼，斷路器開啟）→ 恢復（關閉）→ 停用／啟用 → 編輯設定. With PADOS_E2E_REAL_OLLAMA=1 the
// local Ollama provider shows a measured, authorized model next to unmeasured ones (diagnostics only, no inference).
const live = process.env.PADOS_E2E_PROVIDERS_LIVE === "1";

function env(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required for the live providers spec`);
  return value;
}

async function shot(page: Page, name: string): Promise<void> {
  await page.screenshot({ path: join(env("PADOS_E2E_EVIDENCE_DIR"), `${name}.png`), fullPage: true });
}

async function simMode(mode: string): Promise<void> {
  const context = await apiRequest.newContext();
  const response = await context.post(`${env("PADOS_E2E_SIM_URL")}/__sim/mode`, { data: { mode } });
  expect(response.ok()).toBeTruthy();
  await context.dispose();
}

async function testConnection(card: Locator): Promise<void> {
  await card.getByRole("button", { name: "測試連線", exact: true }).click();
  await expect(card.getByRole("button", { name: "測試連線", exact: true })).toBeEnabled({ timeout: 60_000 });
}

test.describe("AI 模型與連線 (Providers), live", () => {
  test.skip(!live, "needs scripts/dogfood/gateway_simulated_e2e.py --playwright");
  test.setTimeout(240_000);

  test("reachable is shown apart from authorized & measured, failures read in plain words", async ({ page }) => {
    // No response may ever carry the gateway key (the simulated gateway's key is in this process's environment).
    const leaks: string[] = [];
    const gatewayKey = process.env.PADOS_SIM_GATEWAY_KEY || "";
    page.on("response", async (response) => {
      const text = await response.text().catch(() => "");
      if (gatewayKey && text.includes(gatewayKey)) leaks.push(response.url());
    });
    await page.goto("/operator.html");
    await page.getByRole("button", { name: "Control Center" }).click();
    const nav = page.getByRole("navigation", { name: "Control center navigation" });
    await nav.getByRole("button", { name: "AI 模型與連線 (Providers)" }).click();
    const tokenForm = page.getByRole("form", { name: "Control token" });
    await tokenForm.locator("input[name=control-token]").fill(env("PADOS_E2E_CONTROL_VALUE"));
    await tokenForm.locator("input[name=control-token]").press("Enter");
    await expect(page.getByRole("heading", { level: 1, name: "AI 模型與連線 (Providers)" })).toBeVisible();
    await expect(page.getByTestId("governed-use-rule")).toContainText("可連線 ≠ 可用於受治理的工作");

    const card = page.getByTestId("provider-sim-gateway");
    await expect(card).toBeVisible();
    await testConnection(card);
    await expect(card.getByTestId("reach-state")).toHaveText("已連線 Reachable");
    await expect(card.getByTestId("model-sim-small").getByTestId("model-governance")).toHaveText("未授權（尚未測量）");
    await expect(card.getByTestId("governance-summary")).toHaveText("此處沒有任何模型可以執行受治理的工作（未授權）");
    await expect(card).toContainText("請勿在此貼上任何內部文件");
    await shot(page, "01-gateway-reachable-not-authorized");

    await card.getByRole("button", { name: "以探測訊息測試", exact: true }).click();
    await expect(card.getByTestId("check-probe_chat")).toContainText("PASS", { timeout: 60_000 });
    await expect(card.getByTestId("check-probe_chat")).toContainText("非權威觀察結果");
    await shot(page, "02-gateway-probe-non-authoritative");

    await simMode("unavailable");
    await testConnection(card);
    await expect(card.getByTestId("reach-state")).toHaveText("無法連線 Unreachable");
    await expect(card).toContainText("閘道回報伺服器端錯誤或維護中");
    await testConnection(card);
    await expect(card.getByTestId("circuit-state")).toContainText("開啟：連續失敗後暫停呼叫");
    await card.locator(".provider-levels details summary").first().click();
    await expect(card.locator(".provider-levels details code").first()).toHaveText("GATEWAY_UNAVAILABLE");
    await shot(page, "03-gateway-outage-plain-language-circuit-open");

    await simMode("normal");
    await testConnection(card);
    await expect(card.getByTestId("reach-state")).toHaveText("已連線 Reachable");
    await expect(card.getByTestId("circuit-state")).toContainText("關閉：正常放行呼叫");
    await shot(page, "04-gateway-recovered-circuit-closed");

    await card.getByRole("button", { name: "停用", exact: true }).click();
    await expect(card.getByTestId("reach-state")).toHaveText("已停用 Disabled");
    await card.getByRole("button", { name: "啟用", exact: true }).click();
    await expect(card.getByRole("button", { name: "停用", exact: true })).toBeVisible();

    await card.getByRole("button", { name: "編輯設定", exact: true }).click();
    await card.getByLabel("逾時（秒）").fill("20");
    await card.getByRole("button", { name: "儲存設定", exact: true }).click();
    await expect(card).toContainText("已儲存（設定版本");
    await shot(page, "05-gateway-settings-saved");

    if (process.env.PADOS_E2E_REAL_OLLAMA === "1") {
      const ollama = page.getByTestId("provider-ollama-local");
      await testConnection(ollama);
      await expect(ollama.getByTestId("reach-state")).toHaveText("已連線 Reachable");
      await expect(ollama.getByTestId("model-qwen3:8b").getByTestId("model-governance")).toHaveText("已授權並測量 Authorized & measured");
      await expect(ollama.getByTestId("check-governed_binding")).toContainText("BOUND");
      await expect(ollama.getByTestId("governance-summary")).toHaveText("至少一個模型可以執行受治理的工作");
      await shot(page, "06-real-ollama-measured-and-authorized");
    }
    expect(gatewayKey).not.toBe("");
    expect(leaks).toEqual([]);
  });
});
