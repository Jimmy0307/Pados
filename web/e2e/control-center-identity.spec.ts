import { test, expect, type Page, type Request, type Route } from "@playwright/test";

// Mocked API: pairing this browser in the Control Center (docs/identity/UNIFIED_IDENTITY_SESSION_DESIGN.md §3).
// After pairing, the token box is gone and a write carries the session's CSRF header and no control token; a
// reload restores the session; an expired session is reported and the person is asked to pair again; with pairing
// off, the control token box is exactly as before. Fixtures only: no real device is paired by this test.
const projectId = "325f8168-efec-4818-99ac-b00a03d6accd";
const csrf = "synthetic-csrf-value";
const paired = {
  enabled: true, authenticated: true, device_id: "dev_0123456789abcdef", device_label: "Test browser",
  auth_method: "device_pairing", expires_at: "2026-11-17T10:00:00Z", idle_expires_at: "2026-11-16T12:00:00Z",
  csrf_token: csrf, providers: [],
};
const unpaired = { enabled: true, authenticated: false, providers: [] };
const mission = {
  projects: [], projection_only: true, authority: "PADOS_POSTGRESQL_AND_PROJECT_REGISTRY",
  system_health: { alive: true, configured: true, usable: true, ready: true, degraded: false, blocked: false, state: "ready", checks: [] },
};

type Server = { session: Record<string, unknown>; writes: Request[]; workReply?: (route: Route) => Promise<void> };

async function openControlCenter(page: Page, server: Server): Promise<void> {
  await page.route("**/api/**", (route) => route.fulfill({ json: {} }));
  await page.route("**/api/projects", (route) => route.fulfill({ json: { projects: [] } }));
  await page.route("**/api/projects?*", (route) => route.fulfill({ json: { projects: [{ id: projectId, name: "Synthetic" }], pagination: { total: 1 } } }));
  await page.route("**/api/mission-control", (route) => route.fulfill({ json: mission }));
  await page.route("**/api/decision-inbox", (route) => route.fulfill({ json: { count: 0, items: [] } }));
  await page.route("**/identity/v1/session", (route) => route.fulfill({ json: server.session }));
  await page.route("**/identity/v1/pair", (route) => {
    server.writes.push(route.request());
    server.session = paired;
    return route.fulfill({ json: { device_id: paired.device_id, device_label: paired.device_label, csrf_token: csrf,
      expires_at: paired.expires_at, idle_timeout_seconds: 7200 } });
  });
  await page.route("**/control/work-requests", (route) => {
    server.writes.push(route.request());
    return server.workReply ? server.workReply(route)
      : route.fulfill({ json: { status: "ok", work_request: { id: "wwwwwwww-0000-4000-8000-000000000003" } } });
  });
  await page.goto("/operator.html");
  await page.getByRole("button", { name: "Control Center" }).click();
}

function nav(page: Page, name: string) {
  return page.getByRole("navigation", { name: "Control center navigation" }).getByRole("button", { name });
}

async function fillWorkRequest(page: Page): Promise<void> {
  await page.getByLabel("Project").selectOption(projectId);
  await page.getByLabel("Title").fill("Check the docs links");
  await page.getByLabel("Capability it needs").selectOption("RUN_STATIC_ANALYSIS");
  await page.getByLabel("What should be done").fill("Run the markdown reference check on docs/.");
  await page.getByRole("button", { name: "Create work request" }).click();
}

test("a paired browser writes with its session and CSRF header, never the control token, and survives a reload", async ({ page }) => {
  const server: Server = { session: unpaired, writes: [] };
  await openControlCenter(page, server);
  await nav(page, "New work request").click();
  await expect(page.getByRole("button", { name: "Pair this browser or enter the control token first" })).toBeDisabled();
  await expect(page.getByText("in a terminal on this computer")).toBeVisible();
  await page.locator("input[name=pairing-code]").fill("abcde fghjk");
  await expect(page.locator("input[name=pairing-code]")).toHaveValue("ABCDE-FGHJK");
  await page.getByRole("button", { name: "Pair this browser", exact: true }).click();
  await expect(page.getByText("Paired as Test browser (device dev_0123456789abcdef)").first()).toBeVisible();
  await expect(page.locator("input[name=control-token]")).toHaveCount(0);
  expect(server.writes[0].postDataJSON()).toEqual({ pairing_code: "ABCDE-FGHJK", device_label: expect.any(String) });

  await fillWorkRequest(page);
  await expect(page.getByText("Created work request wwwwwwww-0000-4000-8000-000000000003.")).toBeVisible();
  const write = server.writes[1].headers();
  expect(write["x-pados-csrf"]).toBe(csrf);
  expect(write["x-pados-control-token"]).toBeUndefined();

  await page.reload();
  await page.getByRole("button", { name: "Control Center" }).click();
  await nav(page, "New work request").click();
  await expect(page.getByRole("button", { name: "Log out" })).toBeVisible();
  await expect(page.locator("input[name=control-token]")).toHaveCount(0);
  const stored = await page.evaluate(() => [window.localStorage.length, window.sessionStorage.length, window.location.href]);
  expect(stored[0]).toBe(0);
  expect(stored[1]).toBe(0);
  expect(String(stored[2])).not.toContain(csrf);
});

test("an expired session is reported, not retried, and the person is asked to pair again", async ({ page }) => {
  const server: Server = { session: paired, writes: [] };
  server.workReply = (route) => {
    server.session = unpaired;
    return route.fulfill({ status: 401, json: { error: { schema_version: "pados.error.v1", code: "SESSION_EXPIRED",
      message: "session expired", retryable: false, request_id: "srv-1", write_occurred: "NO" } } });
  };
  await openControlCenter(page, server);
  await nav(page, "New work request").click();
  await expect(page.getByRole("button", { name: "Log out" })).toBeVisible();
  await fillWorkRequest(page);
  await expect(page.getByText(/Not created: This browser's session has expired/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Pair this browser", exact: true })).toBeVisible();
  expect(server.writes).toHaveLength(1);
});

test("with pairing off, the control token box is offered as before, with how to turn pairing on", async ({ page }) => {
  const server: Server = { session: { enabled: false, authenticated: false, providers: [] }, writes: [] };
  await openControlCenter(page, server);
  await nav(page, "New work request").click();
  await expect(page.getByText("set PADOS_IDENTITY_SESSIONS_ENABLED=true and restart pados serve")).toBeVisible();
  await expect(page.getByRole("button", { name: "Enter the control token first" })).toBeDisabled();
  await page.locator("input[name=control-token]").fill("synthetic-control-value");
  await page.getByRole("button", { name: "Use token" }).click();
  await fillWorkRequest(page);
  await expect(page.getByText("Created work request wwwwwwww-0000-4000-8000-000000000003.")).toBeVisible();
  expect(server.writes[0].headers()["x-pados-control-token"]).toBe("synthetic-control-value");
  expect(server.writes[0].headers()["x-pados-csrf"]).toBeUndefined();
});
