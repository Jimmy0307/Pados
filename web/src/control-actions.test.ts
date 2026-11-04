// SPDX-License-Identifier: AGPL-3.0-only
// The Control Center's write actions use the existing control API: the same routes, the control token header, the
// server's idempotency and version checks. Nothing is stored in the browser, and the server's refusal of a result
// acceptance (RESULT_ACCEPTANCE_REQUIRES_STEP_UP) reaches the person as the code.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { describeFailure } from "./http";
import {
  CONTROL_TOKEN_HEADER,
  ControlTokenPanel,
  createWorkRequest,
  decideApproval,
  decisionPrompt,
  getApproval,
  type ControlApproval,
} from "./control-actions";

type Sent = { url: string; init: RequestInit };

function recorder(status: number, body: unknown): { sent: Sent[]; fetcher: typeof fetch } {
  const sent: Sent[] = [];
  const fetcher = (async (url: string, init: RequestInit) => {
    sent.push({ url, init });
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }) as unknown as typeof fetch;
  return { sent, fetcher };
}

const approval: ControlApproval = {
  id: "11111111-2222-3333-4444-555555555555", project_id: "p1", status: "PENDING", version: 3,
  proposal_summary: "Run the markdown reference check", subject_kind: "WORK_ROUTE",
  subject_json: { request_id: "r1", actor: { surfaces: ["CONTROL_API"], worker_types: ["COMMAND"] },
    operations: [{ capability_id: "RUN_VALIDATION", command_id: "markdown_reference_check", max_risk_level: "LOW" }] },
};

function headerOf(init: RequestInit, name: string): string | undefined {
  return (init.headers as Record<string, string>)[name];
}

describe("control API actions", () => {
  it("creates a work request on the existing route with the token and a fresh idempotency key", async () => {
    const { sent, fetcher } = recorder(200, { status: "ok", work_request: { id: "w1" } });
    await createWorkRequest("tok-1", "p1", "Check links", "Check the docs links", fetcher);
    await createWorkRequest("tok-1", "p1", "Check links", "Check the docs links", fetcher);
    expect(sent.map((s) => s.url)).toEqual(["/control/work-requests", "/control/work-requests"]);
    expect(headerOf(sent[0].init, CONTROL_TOKEN_HEADER)).toBe("tok-1");
    const [first, second] = sent.map((s) => JSON.parse(String(s.init.body)));
    expect(first).toMatchObject({ project_id: "p1", title: "Check links", body: "Check the docs links" });
    expect(first.idempotency_key).toMatch(/^web-work-[0-9a-f]{24}$/);
    expect(first.idempotency_key).not.toBe(second.idempotency_key);
    expect(JSON.stringify(first)).not.toContain("tok-1");  // the token travels only in its header
  });

  it.each(["approve", "reject", "revise"] as const)("sends %s with the version the person reviewed", async (verb) => {
    const { sent, fetcher } = recorder(200, { status: "ok", approval: { ...approval, status: "APPROVED" } });
    await decideApproval("tok-1", approval, verb, "looks right", fetcher);
    expect(sent[0].url).toBe(`/control/approvals/${approval.id}/${verb}`);
    expect(sent[0].init.method).toBe("POST");
    expect(JSON.parse(String(sent[0].init.body))).toMatchObject({ version: 3, reason: "looks right" });
  });

  it("reads one approval with the token", async () => {
    const { sent, fetcher } = recorder(200, { status: "ok", approval });
    expect((await getApproval("tok-1", approval.id, fetcher)).approval.version).toBe(3);
    expect(headerOf(sent[0].init, CONTROL_TOKEN_HEADER)).toBe("tok-1");
  });

  it("surfaces the server's refusal of a result acceptance as its code", async () => {
    const { fetcher } = recorder(403, { detail: "RESULT_ACCEPTANCE_REQUIRES_STEP_UP" });
    const failure = await decideApproval("tok-1", approval, "approve", "", fetcher).catch((error: unknown) => error);
    expect(describeFailure(failure)).toContain("RESULT_ACCEPTANCE_REQUIRES_STEP_UP");
  });

  it("asks the person to confirm exactly what is decided", () => {
    const prompt = decisionPrompt(approval, "approve");
    for (const part of ["APPROVE", approval.id, "project p1", "version 3", "RUN_VALIDATION markdown_reference_check",
                        "risk up to LOW", "request r1", "Run the markdown reference check"]) {
      expect(prompt).toContain(part);
    }
    expect(decisionPrompt({ ...approval, subject_kind: null, subject_json: null }, "reject")).toContain("cannot route work");
  });
});

describe("the control token stays in page memory", () => {
  it("is a masked field that the browser is told not to remember", () => {
    const html = renderToStaticMarkup(createElement(ControlTokenPanel, { token: "", onChange: () => undefined }));
    expect(html).toContain('type="password"');
    expect(html).toContain('autoComplete="off"');
    expect(html).toMatch(/memory only/);
  });

  it("is never written to browser storage or a cookie", () => {
    for (const name of ["control-actions.tsx", "control-center.tsx"]) {
      const source = readFileSync(fileURLToPath(new URL(name, import.meta.url)), "utf8");
      expect(source, name).not.toMatch(/localStorage|sessionStorage|indexedDB|document\.cookie/);
    }
  });
});
