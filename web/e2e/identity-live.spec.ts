import { test, expect, type Request } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

// Unmocked: a real `pados serve` (PADOS_IDENTITY_SESSIONS_ENABLED=true) and its PostgreSQL behind the Vite proxy.
// Pairs this browser with a code minted by `pados device pair`, reloads and stays paired with no token box, creates a
// Work Request through the UI with the session only, and logs out. scripts/dogfood/identity_e2e.py --playwright runs it
// and provides the environment below; without PADOS_E2E_IDENTITY_LIVE=1 it is skipped.
const live = process.env.PADOS_E2E_IDENTITY_LIVE === "1";
const label = "Playwright live browser";

function env(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required for the live identity spec`);
  return value;
}

function mintPairingCode(python: string, apiUrl: string, tokenFile: string): string {
  const output = execFileSync(python, ["-m", "pados.cli.main", "device", "pair", "--json", "--base-url", apiUrl,
    "--token-file", tokenFile], { encoding: "utf8", env: { ...process.env, PYTHONUTF8: "1" }, timeout: 60_000 });
  const code = String(JSON.parse(output.slice(output.indexOf("{"), output.lastIndexOf("}") + 1)).pairing_code);
  expect(code).toMatch(/^[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}$/);
  return code;
}

test.describe("live identity: pairing and the paired session", () => {
  test.skip(!live, "set PADOS_E2E_IDENTITY_LIVE=1 with a live pados serve (see scripts/dogfood/identity_e2e.py)");

  test("pair with a CLI code, reload without a token, create a work request with the session, log out", async ({ page }) => {
    test.setTimeout(180_000);
    const apiUrl = env("PADOS_E2E_API_URL");
    const tokenFile = env("PADOS_E2E_TOKEN_FILE");
    const projectName = env("PADOS_E2E_PROJECT_NAME");
    const controlValue = readFileSync(tokenFile, "utf8").trim();
    const leaks: string[] = [];
    const writes: Request[] = [];
    page.on("request", (request) => {
      if (request.method() !== "GET" && /\/(control|identity)\//.test(request.url())) writes.push(request);
    });
    page.on("response", async (response) => {
      const body = await response.text().catch(() => "");
      const headers = JSON.stringify(await response.allHeaders().catch(() => ({})));
      if (body.includes(controlValue) || headers.includes(controlValue)) leaks.push(response.url());
    });

    const code = mintPairingCode(env("PADOS_E2E_PYTHON"), apiUrl, tokenFile);
    await page.goto("/operator.html");
    await page.getByRole("button", { name: "Control Center" }).click();
    const nav = page.getByRole("navigation", { name: "Control center navigation" });
    await nav.getByRole("button", { name: "New work request" }).click();
    await expect(page.getByText("Not paired", { exact: true }).first()).toBeVisible();
    await page.locator("input[name=pairing-code]").fill(code.toLowerCase().replace("-", " "));
    await expect(page.locator("input[name=pairing-code]")).toHaveValue(code);
    await page.locator("input[name=device-label]").fill(label);
    await page.getByRole("button", { name: "Pair this browser", exact: true }).click();
    await expect(page.getByText(`Paired as ${label} (device dev_`).first()).toBeVisible();
    await expect(page.locator("input[name=control-token]")).toHaveCount(0);

    await page.reload();
    await page.getByRole("button", { name: "Control Center" }).click();
    await nav.getByRole("button", { name: "New work request" }).click();
    await expect(page.getByText(`Paired as ${label} (device dev_`).first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Log out" })).toBeVisible();
    await expect(page.locator("input[name=control-token]")).toHaveCount(0);

    await page.getByLabel("Project").selectOption({ label: projectName });
    await page.getByLabel("Title").fill("Live session work request");
    await page.getByLabel("Capability it needs").selectOption("RUN_STATIC_ANALYSIS");
    await page.getByLabel("What should be done").fill("Synthetic: check every Markdown reference in docs/.");
    await page.getByRole("button", { name: "Create work request" }).click();
    const created = page.getByText(/^Created work request [0-9a-f-]{36}\.$/);
    await expect(created).toBeVisible();
    const requestId = (await created.textContent())?.match(/[0-9a-f-]{36}/)?.[0];
    const stored = await page.request.get(`${apiUrl}/control/work-requests/${requestId}`,
      { headers: { "X-PADOS-Control-Token": controlValue } });
    expect(stored.status()).toBe(200);
    expect((await stored.json()).work_request.title).toBe("Live session work request");
    const write = writes.find((request) => request.url().endsWith("/control/work-requests"));
    expect(write, "the work request went through the Vite proxy as a browser write").toBeTruthy();
    const sent = await write!.allHeaders();
    expect(sent["x-pados-control-token"]).toBeUndefined();
    expect(sent["x-pados-csrf"]).toBeTruthy();

    const memory = await page.evaluate(() => [window.localStorage.length, window.sessionStorage.length,
      window.location.href, document.documentElement.outerHTML]);
    expect(memory[0]).toBe(0);
    expect(memory[1]).toBe(0);
    expect(String(memory[2])).not.toContain(controlValue);
    expect(String(memory[3])).not.toContain(controlValue);

    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page.getByText(/^Logged out: this browser's session was revoked/).first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Pair this browser", exact: true })).toBeVisible();
    const after = await page.evaluate(async () => (await fetch("/identity/v1/session", { credentials: "same-origin" })).json());
    expect(after.authenticated).toBe(false);
    expect(writes.every((request) => request.headers()["x-pados-control-token"] === undefined)).toBe(true);
    expect(leaks).toEqual([]);
  });
});
