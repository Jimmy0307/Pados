import { test, expect, type Page, type Request } from "@playwright/test";

// Mocked API: the Control Center's local actions. The inbox sends a result acceptance to the Remote console's
// Actions view (never to an API route), reviews a work package approval in the console with the control token and an
// explicit confirmation, and creates a work request on the existing control route. These are fixtures only: no real
// approval is decided by this test.
const projectId = "325f8168-efec-4818-99ac-b00a03d6accd";
const routeApproval = "aaaaaaaa-0000-4000-8000-000000000001";
const resultApproval = "bbbbbbbb-0000-4000-8000-000000000002";
const mission = {
  projects: [], projection_only: true, authority: "PADOS_POSTGRESQL_AND_PROJECT_REGISTRY",
  system_health: { alive: true, configured: true, usable: true, ready: true, degraded: false, blocked: false, state: "ready", checks: [] },
};
const inbox = {
  count: 2, projection_only: true, authority: "PADOS",
  items: [
    { id: `approval:${resultApproval}`, project_id: projectId, project: "Synthetic", item: "Result acceptance for task t1",
      type: "APPROVAL", priority: 90, status: "PENDING", reason_source: "PADOS approval gate",
      required_human_action: "Verified, not accepted yet: approve or reject it in the Remote console (Actions) with a fresh WebAuthn step-up",
      target: "/remote/v1/approvals", projection_only: true,
      open: { surface: "REMOTE_CONSOLE", view: "actions", approval_id: resultApproval, needs: ["REMOTE_SIGN_IN", "ENROLLED_WEBAUTHN_DEVICE"] } },
    { id: `approval:${routeApproval}`, project_id: projectId, project: "Synthetic", item: "Route the reference check",
      type: "APPROVAL", priority: 90, status: "PENDING", reason_source: "PADOS approval gate",
      required_human_action: "Approve, reject, or revise the governed work package",
      target: `/control/approvals/${routeApproval}`, projection_only: true,
      open: { surface: "CONTROL_CENTER", view: "approvals", approval_id: routeApproval, needs: ["CONTROL_TOKEN"] } },
  ],
};
const approval = {
  id: routeApproval, project_id: projectId, status: "PENDING", version: 2, proposal_summary: "Run markdown_reference_check",
  subject_kind: "WORK_ROUTE", expires_at: null,
  subject_json: { request_id: "r1", actor: { surfaces: ["CONTROL_API"], worker_types: ["COMMAND"] },
    operations: [{ capability_id: "RUN_VALIDATION", command_id: "markdown_reference_check", max_risk_level: "LOW" }] },
};

async function openControlCenter(page: Page, writes: Request[]): Promise<void> {
  await page.route("**/api/**", (route) => route.fulfill({ json: {} }));
  await page.route("**/api/projects", (route) => route.fulfill({ json: { projects: [] } }));  // the corpus workspace
  await page.route("**/api/projects?*", (route) => route.fulfill({ json: { projects: [{ id: projectId, name: "Synthetic" }], pagination: { total: 1 } } }));
  await page.route("**/api/mission-control", (route) => route.fulfill({ json: mission }));
  await page.route("**/api/decision-inbox", (route) => route.fulfill({ json: inbox }));
  await page.route(`**/control/approvals/${routeApproval}`, (route) => route.fulfill({ json: { status: "ok", approval } }));
  await page.route(`**/control/approvals/${routeApproval}/approve`, (route) => {
    writes.push(route.request());
    return route.fulfill({ json: { status: "ok", approval: { ...approval, status: "APPROVED", version: 3 } } });
  });
  await page.route("**/control/work-requests", (route) => {
    writes.push(route.request());
    return route.fulfill({ json: { status: "ok", work_request: { id: "wwwwwwww-0000-4000-8000-000000000003" } } });
  });
  await page.goto("/operator.html");
  await page.getByRole("button", { name: "Control Center" }).click();
}

function nav(page: Page, name: string) {
  return page.getByRole("navigation", { name: "Control center navigation" }).getByRole("button", { name });
}

test("a result acceptance opens the Remote console's Actions view and says what is needed", async ({ page }) => {
  await openControlCenter(page, []);
  await nav(page, "Decision / Action Inbox").click();
  const link = page.getByRole("link", { name: "Open in Remote console" });
  await expect(link).toHaveAttribute("href", "/?view=actions");
  await expect(page.getByText(/WebAuthn device enrolled/).first()).toBeVisible();
  await expect(page.locator('a[href="/remote/v1/approvals"]')).toHaveCount(0);
  await link.click();
  await expect(page).toHaveURL(/\/\?view=actions$/);
  await expect(page.getByText("After you connect, the Actions view opens.")).toBeVisible();
  await expect(page.getByText(/Register this device/)).toBeVisible();
});

test("a work package approval is reviewed and decided only after the person confirms it", async ({ page }) => {
  const writes: Request[] = [];
  await openControlCenter(page, writes);
  await nav(page, "Decision / Action Inbox").click();
  await page.getByRole("button", { name: "Review" }).click();
  await expect(page.getByRole("heading", { name: "Approvals", exact: true })).toBeVisible();
  await expect(page.getByText(`Enter the control token above to review approval ${routeApproval}.`)).toBeVisible();
  await page.locator("input[name=control-token]").fill("synthetic-control-value");
  await page.getByRole("button", { name: "Use token" }).click();
  await expect(page.getByText("RUN_VALIDATION markdown_reference_check · risk up to LOW")).toBeVisible();

  let prompt = "";
  page.once("dialog", (dialog) => { prompt = dialog.message(); void dialog.dismiss(); });
  await page.getByRole("button", { name: "Approve" }).click();
  await expect.poll(() => prompt).toContain(`APPROVE approval ${routeApproval}`);
  expect(writes).toHaveLength(0);  // dismissed: nothing sent

  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("Recorded: APPROVED.")).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0].headers()["x-pados-control-token"]).toBe("synthetic-control-value");
  expect(writes[0].postDataJSON()).toMatchObject({ version: 2 });
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }) + document.cookie);
  expect(stored).not.toContain("synthetic-control-value");
});

test("a result acceptance has no decide buttons in the Control Center", async ({ page }) => {
  await openControlCenter(page, []);
  await nav(page, "Approvals").click();
  const row = page.getByRole("row").filter({ hasText: "Result acceptance for task t1" });
  await expect(row.getByRole("link", { name: "Open in Remote console" })).toBeVisible();
  await expect(row.getByRole("button")).toHaveCount(0);
});

test("a work request is created on the existing control route", async ({ page }) => {
  const writes: Request[] = [];
  await openControlCenter(page, writes);
  await nav(page, "New work request").click();
  await expect(page.getByRole("button", { name: "Enter the control token first" })).toBeDisabled();
  await page.locator("input[name=control-token]").fill("synthetic-control-value");
  await page.getByRole("button", { name: "Use token" }).click();
  await page.getByLabel("Project").selectOption(projectId);
  await page.getByLabel("Title").fill("Check the docs links");
  await page.getByLabel("Capability it needs").selectOption("RUN_STATIC_ANALYSIS");
  await page.getByLabel("What should be done").fill("Run the markdown reference check on docs/.");
  await page.getByRole("button", { name: "Create work request" }).click();
  await expect(page.getByText("Created work request wwwwwwww-0000-4000-8000-000000000003.")).toBeVisible();
  expect(writes[0].postDataJSON()).toMatchObject({ project_id: projectId, title: "Check the docs links",
    requested_capability: "RUN_STATIC_ANALYSIS" });
  expect(writes[0].headers()["x-pados-control-token"]).toBe("synthetic-control-value");
});
