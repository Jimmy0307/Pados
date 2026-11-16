import { test, expect, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

// Unmocked cold start: a real `pados serve` (identity sessions on), and a Vite dev server started by
// scripts/dogfood/vite_cold_start_e2e.py from an EMPTY optimizer cache (web/node_modules/.vite removed) just before this
// spec, after the startup readiness condition (GET /__pados/dev-ready + the API's /health/ready). Pairs on the very
// first page load, exactly as web/e2e/product-flow-live.spec.ts starts, and proves the page was never reloaded or
// navigated and no Vite update arrived while the single-use code was in flight. Skipped unless the driver sets the env.
const live = process.env.PADOS_E2E_COLD_START_LIVE === "1";

function env(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required for the live cold-start spec`);
  return value;
}

function mintPairingCode(): string {
  const output = execFileSync(env("PADOS_E2E_PYTHON"), ["-m", "pados.cli.main", "device", "pair", "--json", "--base-url",
    env("PADOS_E2E_API_URL"), "--token-file", env("PADOS_E2E_TOKEN_FILE")], { encoding: "utf8", env: { ...process.env, PYTHONUTF8: "1" }, timeout: 60_000 });
  return String(JSON.parse(output.slice(output.indexOf("{"), output.lastIndexOf("}") + 1)).pairing_code);
}

type Event = { at: number; kind: string; detail: string };

/** Everything that can end or replace the page while a request is in flight, with a timestamp. */
function watch(page: Page, started: number): Event[] {
  const events: Event[] = [];
  const add = (kind: string, detail: string) => events.push({ at: Date.now() - started, kind, detail: detail.slice(0, 300) });
  page.on("framenavigated", (frame) => { if (frame === page.mainFrame()) add("navigated", frame.url()); });
  page.on("console", (message) => { if (/\[vite\]|reload|optimized/i.test(message.text())) add("console", message.text()); });
  page.on("websocket", (socket) => socket.on("framereceived", (frame) => {
    const payload = String(frame.payload);
    if (!payload.includes('"type":"connected"') && !payload.includes('"type":"ping"')) add("vite-ws", payload);
  }));
  page.on("request", (request) => { if (request.url().includes("/identity/v1/pair")) add("pair-request", request.method()); });
  page.on("requestfinished", (request) => {
    if (request.url().includes("/identity/v1/pair")) void request.response().then((r) => add("pair-finished", String(r?.status())));
  });
  page.on("requestfailed", (request) => { if (request.url().includes("/identity/v1/")) add("identity-failed", `${request.url()} ${request.failure()?.errorText}`); });
  return events;
}

test.describe("live cold start pairing", () => {
  test.skip(!live, "run scripts/dogfood/vite_cold_start_e2e.py (it starts the API and a cold Vite dev server)");

  test("pairs on the first page load of a cold dev server with no reload in flight", async ({ page }) => {
    test.setTimeout(120_000);
    const started = Date.now();
    const events = watch(page, started);
    const label = `Cold start ${process.env.PADOS_E2E_ITERATION ?? "0"}`;
    try {
      await page.goto("/operator.html");
      await page.getByRole("button", { name: "Control Center" }).click();
      await page.getByRole("navigation", { name: "Control center navigation" }).getByRole("button", { name: "專案 Projects" }).click();
      await expect(page.locator("input[name=pairing-code]")).toBeVisible();
      await page.locator("input[name=pairing-code]").fill(mintPairingCode());
      await page.locator("input[name=device-label]").fill(label);
      await page.getByRole("button", { name: "Pair this browser", exact: true }).click();
      await expect(page.getByText(`Paired as ${label} (device dev_`).first()).toBeVisible({ timeout: 20_000 });
    } finally {
      writeFileSync(join(env("PADOS_E2E_EVIDENCE_DIR"), `cold-start-events-${process.env.PADOS_E2E_ITERATION ?? "0"}.json`),
        JSON.stringify(events, null, 2), "utf8");
    }
    // One navigation (the goto), no Vite full reload or update, exactly one pair request and it was answered.
    expect(events.filter((e) => e.kind === "navigated")).toHaveLength(1);
    expect(events.filter((e) => e.kind === "vite-ws")).toEqual([]);
    expect(events.filter((e) => e.kind === "pair-request")).toHaveLength(1);
    expect(events.filter((e) => e.kind === "identity-failed")).toEqual([]);
  });
});
