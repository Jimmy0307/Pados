// SPDX-License-Identifier: AGPL-3.0-only
// The product work loop's client calls the existing control routes with the session (CSRF, no token) or the token,
// in the governed order: goal, plan, loop, start; and the page states render with plain words and technical codes.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CONTROL_TOKEN_HEADER, CSRF_HEADER, type SessionAuth } from "./control-auth";
import { HttpError } from "./http";
import { LocaleContext } from "./i18n";
import {
  createCapturedRequest, createProject, decide, loadEvidence, proposePlan, startApprovedPlan, submitCandidate, writeDocument,
} from "./product-api";
import { buildPlanTemplate } from "./product-flow";
import { Blocked, Disabled, Failed, MarkdownPreview } from "./product-ui";

type Sent = { url: string; init: RequestInit };
const session: SessionAuth = { kind: "session", csrfToken: "csrf-1", deviceId: "dev_1" };

function router(routes: Record<string, unknown>, status: Record<string, number> = {}): { sent: Sent[]; fetcher: typeof fetch } {
  const sent: Sent[] = [];
  const fetcher = (async (url: string, init: RequestInit) => {
    sent.push({ url, init });
    const match = Object.keys(routes).find((prefix) => url.startsWith(prefix)) || "";
    return new Response(JSON.stringify(routes[match] ?? {}), { status: status[match] ?? 200, headers: { "Content-Type": "application/json" } });
  }) as unknown as typeof fetch;
  return { sent, fetcher };
}

const header = (init: RequestInit, name: string) => (init.headers as Record<string, string>)[name];
const body = (sent: Sent) => JSON.parse(String(sent.init.body));

beforeEach(() => { vi.stubGlobal("location", { hostname: "127.0.0.1" }); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("product API client", () => {
  it("creates a project with a name only, through the session (CSRF, no token)", async () => {
    const { sent, fetcher } = router({ "/control/projects": { project: { id: "p1", name: "示範" }, replayed: false } });
    await createProject(session, "示範", "說明", "web-project-1", fetcher);
    expect(sent[0].url).toBe("/control/projects");
    expect(body(sent[0])).toEqual({ name: "示範", description: "說明", idempotency_key: "web-project-1" });
    expect(header(sent[0].init, CSRF_HEADER)).toBe("csrf-1");
    expect(header(sent[0].init, CONTROL_TOKEN_HEADER)).toBeUndefined();
    expect(sent[0].init.credentials).toBe("same-origin");
  });

  it("writes a document; a replace carries the SHA-256 it read", async () => {
    const { sent, fetcher } = router({ "/control/projects/p1/documents": { document: { path: "guide.md" } } });
    await writeDocument("tok", "p1", { path: "guide.md", content: "# x" }, "k1", fetcher);
    await writeDocument("tok", "p1", { path: "guide.md", content: "# y", replace: true, expectedSha256: "a".repeat(64) }, "k2", fetcher);
    expect(body(sent[0])).toEqual({ path: "guide.md", content: "# x", replace: false, expected_sha256: null, idempotency_key: "k1" });
    expect(body(sent[1])).toMatchObject({ replace: true, expected_sha256: "a".repeat(64) });
    expect(header(sent[0].init, CONTROL_TOKEN_HEADER)).toBe("tok");
  });

  it("captures the request with its capability and proposes the template as a WORK_ROUTE approval", async () => {
    const { sent, fetcher } = router({ "/control/work-requests": { work_request: { id: "r1", body: "{}" } }, "/control/approvals": { approval: { id: "a1" } } });
    await createCapturedRequest(session, "p1", "檢查", "請檢查", "RUN_STATIC_ANALYSIS", "web-work-1", fetcher);
    const plan = buildPlanTemplate({ projectId: "p1", requestId: "r1", rootGoalId: "goal-root:1", title: "檢查", text: "請檢查" });
    await proposePlan(session, "p1", plan, "web-plan-1", fetcher);
    expect(body(sent[0])).toEqual({ project_id: "p1", title: "檢查", body: "請檢查", idempotency_key: "web-work-1", requested_capability: "RUN_STATIC_ANALYSIS" });
    expect(body(sent[1])).toEqual({ project_id: "p1", proposal_summary: plan.proposalSummary, subject: plan.subject, idempotency_key: "web-plan-1" });
  });

  it("decides with the approval's version and a fresh key", async () => {
    const { sent, fetcher } = router({ "/control/approvals/a1/approve": { approval: { id: "a1", status: "APPROVED" } } });
    await decide(session, { id: "a1", status: "PENDING", version: 2 }, "approve", "", fetcher);
    expect(sent[0].url).toBe("/control/approvals/a1/approve");
    expect(body(sent[0])).toMatchObject({ version: 2, reason: "" });
    expect(body(sent[0]).idempotency_key).toMatch(/^web-approve-[0-9a-f]{24}$/);
  });

  it("after approval creates the goal, the plan and the TaskLoop, then starts it, in that order", async () => {
    const { sent, fetcher } = router({
      "/control/task-goals": { task_goal: { id: "g1" } }, "/control/task-plans": { task_plan: { id: "t1" } },
      "/control/loops/l1/start": { loop: { id: "l1", status: "RUNNING" } }, "/control/loops": { loop: { id: "l1", status: "READY" } },
    });
    const plan = buildPlanTemplate({ projectId: "p1", requestId: "r1", rootGoalId: "goal-root:1", title: "t", text: "x" });
    const loop = await startApprovedPlan(session, { projectId: "p1", requestId: "r1", approvalId: "a1", plan }, fetcher);
    expect(loop.status).toBe("RUNNING");
    expect(sent.map((s) => s.url)).toEqual(["/control/task-goals", "/control/task-plans", "/control/loops", "/control/loops/l1/start"]);
    expect(body(sent[0])).toMatchObject({ project_id: "p1", request_id: "r1", approval_id: "a1" });
    expect(body(sent[1])).toMatchObject({ task_goal_id: "g1", approval_id: "a1", steps: plan.steps });
    expect(body(sent[2])).toMatchObject({ task_goal_id: "g1", active_plan_id: "t1", approval_id: "a1", max_iterations: 4 });
  });

  it("loads the latest job's artifacts and verified texts as evidence", async () => {
    const { sent, fetcher } = router({
      "/control/loops/l1/iterations": { iterations: [{ id: "i1", iteration_number: 1, job_id: "j1", status: "DECIDED" }] },
      "/control/jobs/j1/artifacts/stderr/content": { artifact: { artifact_type: "stderr", content: "{\"broken\":[]}", integrity: "VERIFIED", sha256: "b" } },
      "/control/jobs/j1/artifacts/stdout/content": { artifact: { artifact_type: "stdout", content: "{}", integrity: "VERIFIED", sha256: "c" } },
      "/control/jobs/j1/artifacts": { artifacts: [{ artifact_type: "stderr", sha256: "b" }] },
    });
    const evidence = await loadEvidence(session, "l1", fetcher);
    expect(evidence?.jobId).toBe("j1");
    expect(evidence?.stderr?.integrity).toBe("VERIFIED");
    expect(sent.every((s) => (s.init.method || "GET") === "GET")).toBe(true);
  });

  it("keeps the server's refusal of a candidate and of a decision as closed codes", async () => {
    const { fetcher } = router({ "/control/loops/l1/result-candidate": { detail: "RESULT_CANDIDATE_REQUIRES_COMPLETED_TASKLOOP" } },
      { "/control/loops/l1/result-candidate": 403 });
    await expect(submitCandidate(session, "l1", fetcher)).rejects.toMatchObject({ code: "RESULT_CANDIDATE_REQUIRES_COMPLETED_TASKLOOP", status: 403 });
  });
});

describe("product states", () => {
  const render = (element: ReturnType<typeof createElement>, locale: "zh-TW" | "en" = "zh-TW") =>
    renderToStaticMarkup(createElement(LocaleContext.Provider, { value: locale }, element));

  it("shows a failure in plain words with the technical code on demand", () => {
    const html = render(createElement(Failed, { error: new HttpError("SERVER_ERROR", 403, "RESULT_ACCEPTANCE_REQUIRES_STEP_UP", "req-9", false, "NO") }));
    expect(html).toContain("接受結果需要 Remote WebAuthn step-up");
    expect(html).toContain("<code>RESULT_ACCEPTANCE_REQUIRES_STEP_UP</code>");
    expect(html).toContain("req-9");
  });

  it("shows blocked and disabled states with their reason and next step", () => {
    const blocked = render(createElement(Blocked, { message: "等待", next: "修正文件" }), "en");
    expect(blocked).toContain("Blocked");
    expect(blocked).toContain("Next step");
    expect(render(createElement(Disabled, { reason: "only completed loops" }))).toContain("目前無法使用");
  });

  it("previews Markdown as text, never as HTML", () => {
    const html = render(createElement(MarkdownPreview, { source: "# T\n<img src=x onerror=alert(1)>\n[a](missing.md)" }));
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
    expect(html).toContain("missing.md");
  });
});
