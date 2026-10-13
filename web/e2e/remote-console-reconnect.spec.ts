import { expect, test, devices } from "@playwright/test";

test.use({ ...devices["Pixel 7"] });

const run = { run_id: "run-w", project_id: "proj-a", goal: "Offline job", bucket: "WAITING_APPROVAL",
  progress_status: "VERIFIED", semantic_commit_status: "NOT_COMMITTED", approval_ref: "appr-1" };
const attention = { needs_attention: [run], running: [], waiting_approval: [run], failed: [], completed: [],
  projection: "PADOS_READ_MODEL_NOT_AUTHORITY" };
const workMap = { projects: ["proj-a"], tasks: [], runs: [run], projection: "REBUILDABLE_PADOS_PROJECTION_NOT_AUTHORITY", semantic_authority: "PADOS_HOME_BASE" };

test("transport loss shows reconnecting, WAN loss shows home base running, reconnect refetches", async ({ page }) => {
  let transportUp = true;
  let wan = "OFFLINE";
  let attentionCalls = 0;
  await page.route("**/remote/v1/**", async (route) => {
    if (!transportUp) return route.abort("internetdisconnected");
    const path = new URL(route.request().url()).pathname;
    if (path === "/remote/v1/attention") { attentionCalls += 1; return route.fulfill({ json: attention }); }
    if (path === "/remote/v1/work-map") return route.fulfill({ json: workMap });
    if (path === "/remote/v1/nodes") return route.fulfill({ json: { status: "OK", nodes: [], model_endpoints_exposed: false } });
    if (path === "/remote/v1/connectivity") {
      return route.fulfill({ json: { home_base: "RUNNING", links: { WAN: { link: "WAN", state: wan },
        REMOTE_TRANSPORT: { link: "REMOTE_TRANSPORT", state: "CONNECTED" } } } });
    }
    return route.fulfill({ status: 404, json: { detail: "not mocked" } });
  });

  await page.goto("/");
  await page.getByLabel("Actor").fill("owner");
  await page.getByLabel("Access proof").fill("synthetic-proof");
  await page.getByLabel("Confirm Home Base origin").check();
  await page.getByRole("button", { name: "Connect" }).click();
  const banner = page.getByRole("region", { name: "Connectivity" });
  await expect(banner).toContainText("HOME BASE RUNNING / WAN OFFLINE");

  transportUp = false;
  await page.getByRole("button", { name: "Refresh" }).click();
  await expect(banner).toContainText("RECONNECTING");
  await expect(banner).not.toContainText(/dead|failed/i);
  await page.getByRole("navigation", { name: "PADOS views" }).getByRole("button", { name: "Attention" }).click();
  await expect(page.getByRole("region", { name: "Needs attention" })).toContainText("Offline job");

  transportUp = true;
  wan = "CONNECTED";
  const before = attentionCalls;
  await expect(banner).toContainText("HOME BASE ONLINE / REMOTE CONNECTED", { timeout: 12_000 });
  expect(attentionCalls).toBeGreaterThan(before);

  await page.context().setOffline(true);
  await expect(banner).toContainText("DISCONNECTED");
  await page.context().setOffline(false);
  await expect(banner).toContainText("HOME BASE ONLINE", { timeout: 12_000 });

  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
  expect(stored).not.toContain("Offline job");
  expect(stored).not.toContain("synthetic-proof");
});
