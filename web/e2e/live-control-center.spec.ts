import { test, expect } from "@playwright/test";

// Unmocked acceptance against a running PADOS API (Vite proxies /api to it).
// PADOS_E2E_LIVE=ready expects live authority; =unavailable expects the API down.
const mode = process.env.PADOS_E2E_LIVE;

test.describe("live control center", () => {
  test.skip(!mode, "set PADOS_E2E_LIVE=ready|unavailable with a PADOS API behind the Vite proxy");

  test("Mission Control, projects and inbox render real authority state", async ({ page }) => {
    test.skip(mode !== "ready", "requires a live PADOS API");
    const mission = await (await page.request.get("/api/mission-control")).json();
    const inbox = await (await page.request.get("/api/decision-inbox")).json();
    const project = mission.projects.find((item: { project: { name: string } }) => item.project.name === "PADOS");
    expect(project, "a registered PADOS project must exist in authority").toBeTruthy();

    await page.goto("/operator.html");
    await page.getByRole("button", { name: "Control Center" }).click();
    await expect(page.getByRole("heading", { name: "Mission Control" })).toBeVisible();
    await expect(page.getByRole("heading", { name: `System health · ${mission.system_health.state.toUpperCase()}` })).toBeVisible();
    const card = page.locator("section.control-card", { has: page.getByRole("heading", { name: "PADOS", exact: true }) });
    await expect(card.getByText(project.current_goal.objective)).toBeVisible();
    await expect(card.getByText(project.blockers[0].description)).toBeVisible();
    for (const decision of project.next_decisions.slice(0, 3)) {
      await expect(card.getByText(decision.question)).toBeVisible();
    }
    await expect(card.getByText("Next decisions")).toBeVisible();
    await expect(card.getByText(`Scope ${project.recent_accepted_results[0].accepted_scope_version}`)).toBeVisible();

    await page.getByRole("navigation", { name: "Control center navigation" }).getByRole("button", { name: "Decision / Action Inbox" }).click();
    await expect(page.getByText(`${inbox.count} human actions derived from project, run, and decision state`)).toBeVisible();
    for (const item of inbox.items.filter((entry: { project: string }) => entry.project === "PADOS")) {
      await expect(page.getByRole("row").filter({ hasText: item.item.slice(0, 60) }).first()).toBeVisible();
    }

    await page.getByRole("navigation", { name: "Control center navigation" }).getByRole("button", { name: "Projects" }).click();
    await expect(page.getByRole("cell", { name: /PADOS/ }).first()).toBeVisible();
  });

  test("unavailable authority is shown as unavailable, never as empty success", async ({ page }) => {
    test.skip(mode !== "unavailable", "requires the PADOS API to be stopped");
    await page.goto("/operator.html");
    await page.getByRole("button", { name: "Control Center" }).click();
    await expect(page.getByText("Surface unavailable")).toBeVisible();
    await expect(page.getByText("No registered projects are available from PADOS.")).toHaveCount(0);
  });
});
