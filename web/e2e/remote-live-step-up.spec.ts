import { readFileSync } from "node:fs";
import { expect, test, devices } from "@playwright/test";

/**
 * Live: the PWA against a running Home Base (`python -m pados.remote.serve`, real Postgres lanes)
 * proxied by the dev server. Approval uses a real WebAuthn ceremony with user verification from a
 * Chromium virtual authenticator holding the enrolled key. Enabled only when
 * PADOS_LIVE_REMOTE_STEP_UP points at the seed's browser.json (0600, outside the repository).
 */
const seedFile = process.env.PADOS_LIVE_REMOTE_STEP_UP || "";
test.use({ ...devices["Pixel 7"] });

test("live: mobile PWA approves a verified result with fresh step-up through the real Home Base", async ({ page }) => {
  test.skip(!seedFile, "set PADOS_LIVE_REMOTE_STEP_UP to run against a live Home Base");
  const seed = JSON.parse(readFileSync(seedFile, "utf8")) as { actor: string; access: string; pkcs8: string; key_handle: string };
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  const { authenticatorId } = await cdp.send("WebAuthn.addVirtualAuthenticator", { options: {
    protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true,
    isUserVerified: true, automaticPresenceSimulation: true } });
  await cdp.send("WebAuthn.addCredential", { authenticatorId, credential: {
    credentialId: seed.key_handle, isResidentCredential: false, rpId: "localhost", privateKey: seed.pkcs8, signCount: 0 } });
  const dialogs: string[] = [];
  page.on("dialog", (dialog) => { dialogs.push(dialog.message()); void dialog.accept(); });
  const decisions: string[] = [];
  page.on("request", (request) => { if (request.method() === "POST") decisions.push(new URL(request.url()).pathname); });

  await page.goto("http://localhost:5173/");
  await page.getByLabel("Actor").fill(seed.actor);
  await page.getByLabel("Access proof").fill(seed.access);
  await page.getByLabel("Confirm Home Base origin").check();
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.getByText("HOME BASE ONLINE / REMOTE CONNECTED")).toBeVisible();

  await page.getByRole("navigation", { name: "PADOS views" }).getByRole("button", { name: "Actions" }).click();
  await expect(page.getByRole("region", { name: "Actions" })).toContainText("Result for task:");
  await page.getByRole("button", { name: "Approve" }).first().click();
  await expect(page.getByRole("status")).toContainText("approve: COMMITTED", { timeout: 20_000 });
  expect(dialogs[0]).toContain("APPROVE result candidate:");
  expect(decisions.filter((path) => path.startsWith("/remote/v1/"))).toEqual(expect.arrayContaining([
    "/remote/v1/sessions", expect.stringMatching(/\/step-up$/), expect.stringMatching(/\/verify$/),
    expect.stringMatching(/\/approve$/)]));
  await page.getByRole("navigation", { name: "PADOS views" }).getByRole("button", { name: "Actions" }).click();
  await expect(page.getByRole("region", { name: "Actions" })).toContainText("No decisions pending.");
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
  expect(stored).not.toContain(seed.access);
  expect(stored).not.toContain("step-up-proof:");
});
