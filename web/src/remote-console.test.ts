// SPDX-License-Identifier: AGPL-3.0-only
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { RemoteClient, projectsOf, type AttentionProjection, type RemoteRun, type WorkMapProjection } from "./remote-api";
import { ActionsView, AttentionView, JobsView, NodesView, ProjectsView, REMOTE_VIEWS, TaskAssignmentView, WorkMapView } from "./remote-console";

const waiting: RemoteRun = { run_id: "run-w", project_id: "proj-a", goal: "Tighten README", bucket: "WAITING_APPROVAL",
  progress_status: "VERIFIED", semantic_commit_status: "NOT_COMMITTED", approval_ref: "appr-1" };
const failed: RemoteRun = { run_id: "run-f", project_id: "proj-b", goal: "Fix build", bucket: "FAILED", progress_status: "FAILED" };
const running: RemoteRun = { run_id: "run-r", project_id: "proj-a", goal: "Index docs", bucket: "RUNNING", progress_status: "WORKING" };
const view: AttentionProjection = { needs_attention: [waiting, failed], running: [running], waiting_approval: [waiting],
  failed: [failed], completed: [], projection: "PADOS_READ_MODEL_NOT_AUTHORITY" };
const workMap: WorkMapProjection = {
  projects: ["proj-a"],
  tasks: [{ request_id: "req-1", task_id: "task-1", project_id: "proj-a", original_intent: "Prepare and verify the memo",
    classification: "INTERNAL", priority: "HIGH", deadline: null, scope_version_ref: "scope:1", decomposition_status: "PENDING",
    work_request_status: "PENDING_WORKER_ROUTER", created_at: "2026-10-05T00:00:00Z",
    goals: [{ goal_id: "goal-1", title: "Prepare and verify the memo", objective: "Prepare and verify the memo", state: "PENDING", required_capabilities: [], dependencies: [] }],
    assignments: [], artifacts: [], evidence: [], receipts: [] }],
  runs: [], projection: "REBUILDABLE_PADOS_PROJECTION_NOT_AUTHORITY", semantic_authority: "PADOS_HOME_BASE",
};

describe("remote console", () => {
  it("has the four mobile control views", () => {
    expect(REMOTE_VIEWS.map((v) => v.label)).toEqual(["Map", "Tasks", "Attention", "Actions"]);
  });

  it("renders the attention projection buckets", () => {
    const html = renderToStaticMarkup(createElement(AttentionView, { view }));
    for (const title of ["Needs attention", "Running", "Waiting approval", "Failed", "Completed"]) expect(html).toContain(title);
    expect(html).toContain("Tighten README");
    expect(html).toContain("Nothing here.");
  });

  it("offers approve/reject only for pending decisions", () => {
    const html = renderToStaticMarkup(createElement(ActionsView, { view }));
    expect(html).toContain("Approve");
    expect(html).toContain("Reject");
    expect(html).not.toContain("Fix build");
  });

  it("offers pause/cancel only for running jobs", () => {
    const html = renderToStaticMarkup(createElement(JobsView, { view, onControl: () => undefined }));
    expect(html.match(/Cancel/g)?.length).toBe(1);
  });

  it("groups projects and shows node health without endpoints", () => {
    expect(projectsOf(view).map((p) => p.project_id)).toEqual(["proj-a", "proj-b"]);
    expect(renderToStaticMarkup(createElement(ProjectsView, { view }))).toContain("1 waiting");
    const nodes = renderToStaticMarkup(createElement(NodesView, { nodes: { status: "OK", model_endpoints_exposed: false,
      nodes: [{ node_id: "n1", node_name: "rtx-node", state: "ONLINE", stale: false, authorized: true }] } }));
    expect(nodes).toContain("rtx-node");
    expect(nodes).not.toContain("11434");
  });

  it("sends intent only: START_TASK body has no context or history", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ intent_id: "x", outcome: "SUBMITTED", ledger_entry_hash: "h" })));
    const client = new RemoteClient({ actor: "owner", proof: "p" }, fetcher as unknown as typeof fetch);
    await client.startTask("proj-a", "summarise status");
    const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/remote/v1/intents");
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(["human_message", "intent_id", "intent_type", "project_id"]);
    expect((init.headers as Record<string, string>)["X-PADOS-Remote-Actor"]).toBe("owner");
  });

  it("projects captured task lineage and labels missing assignments", () => {
    const html = renderToStaticMarkup(createElement(WorkMapView, {
      projection: workMap, selectedId: "goal-1", onSelect: () => undefined,
    }));
    expect(html).toContain("PADOS WORK MAP");
    expect(html).toContain("Prepare and verify the memo");
    expect(html).toContain("Not assigned");
    expect(html).toContain("Not allocated");
    expect(html).toContain("Why selected");
    expect(html).toContain("Selection reason is not recorded");
  });

  it("offers intent, project, priority, and optional deadline in task creation", () => {
    const html = renderToStaticMarkup(createElement(TaskAssignmentView, { tasks: workMap.tasks, projects: workMap.projects, onCreate: async () => true }));
    expect(html).toContain("Original intent");
    expect(html).toContain("Project");
    expect(html).toContain("Priority");
    expect(html).toContain("Deadline");
    expect(html).toContain("pending root goal");
  });

  const challenge = {
    challenge_id: "c-1", expires_at: "2026-10-07T00:00:00Z", binding_digest: "b".repeat(64),
    binding: { action: "APPROVE", project_id: "proj-a", task_id: "run-w", goal_id: null, candidate_id: "run-w",
      candidate_digest: "d".repeat(64), classification: "INTERNAL", expected_scope_version: 3, kind: "PRODUCT_RUN" },
    public_key: { challenge: "AAAA", rpId: "localhost", timeout: 120000, userVerification: "required",
      allowCredentials: [{ type: "public-key", id: "Y3JlZA" }] },
  };
  const assertion = { credential_id: "Y3JlZA", client_data_json: "e30", authenticator_data: "AA", signature: "AA" };

  function stepUpFetcher(binding = challenge.binding) {
    return vi.fn(async (url: string) => {
      const path = String(url);
      if (path.endsWith("/sessions")) return new Response(JSON.stringify({ session_handle: "h".repeat(43) }));
      if (path.endsWith("/step-up")) return new Response(JSON.stringify({ ...challenge, binding }));
      if (path.endsWith("/verify")) return new Response(JSON.stringify({ proof_ref: "step-up-proof:1" }));
      return new Response(JSON.stringify({ intent_id: "x", outcome: "COMMITTED", ledger_entry_hash: "h" }));
    });
  }

  it("approve runs a fresh step-up bound to the presented candidate and sends the single-use proof", async () => {
    const fetcher = stepUpFetcher();
    const authenticator = vi.fn(async () => assertion);
    const client = new RemoteClient({ actor: "owner", proof: "p" }, fetcher as unknown as typeof fetch);
    const outcome = await client.decide(waiting, "approve", authenticator);
    expect(outcome.outcome).toBe("COMMITTED");
    const urls = fetcher.mock.calls.map((call) => String((call as unknown as [string])[0]));
    expect(urls).toEqual(["/remote/v1/sessions", "/remote/v1/approvals/appr-1/step-up",
      "/remote/v1/approvals/appr-1/step-up/c-1/verify", "/remote/v1/approvals/appr-1/approve"]);
    expect(authenticator).toHaveBeenCalledWith(challenge.public_key);
    const stepUp = (fetcher.mock.calls[1] as unknown as [string, RequestInit])[1];
    expect(JSON.parse(String(stepUp.body))).toEqual({ action: "APPROVE" });  // no client-chosen binding
    const [, init] = fetcher.mock.calls[3] as unknown as [string, RequestInit];
    const body = JSON.parse(String(init.body)) as Record<string, unknown>;
    expect(body.target_run_id).toBe("run-w");
    expect(body.step_up_proof).toBe("step-up-proof:1");
    expect((init.headers as Record<string, string>)["X-PADOS-Remote-Session"]).toBe("h".repeat(43));
  });

  it("refuses a challenge bound to another candidate and a declined confirmation", async () => {
    const authenticator = vi.fn(async () => assertion);
    const swapped = stepUpFetcher({ ...challenge.binding, candidate_id: "other", task_id: "other" });
    await expect(new RemoteClient({ actor: "owner", proof: "p" }, swapped as unknown as typeof fetch)
      .decide(waiting, "approve", authenticator)).rejects.toThrow("STEP_UP_BINDING_MISMATCH");
    const declined = stepUpFetcher();
    await expect(new RemoteClient({ actor: "owner", proof: "p" }, declined as unknown as typeof fetch)
      .decide(waiting, "approve", authenticator, () => false)).rejects.toThrow("STEP_UP_DECLINED");
    expect(authenticator).not.toHaveBeenCalled();
    expect(declined.mock.calls.some((call) => String((call as unknown as [string])[0]).endsWith("/approve"))).toBe(false);
  });
});

describe("remote endpoint configuration", () => {
  it("uses the configured Home Base origin without storing the access proof", async () => {
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ projection: "ok" })));
    const client = new RemoteClient({ actor: "owner", proof: "private-proof" }, fetcher as unknown as typeof fetch,
      "https://home-base.example");
    await client.workMap();
    const [url, init] = fetcher.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://home-base.example/remote/v1/work-map");
    expect(JSON.stringify({ url, body: init.body })).not.toContain("private-proof");
    expect((init.headers as Record<string, string>)["X-PADOS-Remote-Proof"]).toBe("private-proof");
  });

  it("rejects insecure remote endpoints", () => {
    expect(() => new RemoteClient({ actor: "owner", proof: "p" }, fetch, "http://home-base.example"))
      .toThrow("REMOTE_ENDPOINT_INVALID");
  });
});

describe("connectivity banner", () => {
  it("never presents transport loss as Home Base failure", async () => {
    const { deriveBanner } = await import("./remote-api");
    const { ConnectivityBanner } = await import("./remote-console");
    expect(deriveBanner({ browserOnline: false, reachable: false }).text).toContain("DISCONNECTED");
    expect(deriveBanner({ browserOnline: true, reachable: false }).text).toContain("RECONNECTING");
    const wanDown = deriveBanner({ browserOnline: true, reachable: true,
      connectivity: { home_base: "RUNNING", links: { WAN: { link: "WAN", state: "OFFLINE" } } } });
    expect(wanDown.text).toBe("HOME BASE RUNNING / WAN OFFLINE");
    expect(deriveBanner({ browserOnline: true, reachable: true, connectivity: null }).text)
      .toBe("HOME BASE ONLINE / REMOTE CONNECTED");
    for (const banner of [wanDown, deriveBanner({ browserOnline: true, reachable: false })]) {
      const html = renderToStaticMarkup(createElement(ConnectivityBanner, { banner, lastSync: "10:00" }));
      expect(html).not.toMatch(/dead|failed|down/i);
      expect(html).toContain("not cached on this device");
    }
  });
});
