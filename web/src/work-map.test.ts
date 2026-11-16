// SPDX-License-Identifier: AGPL-3.0-only
// 工作心智圖: the tree is projected from the backend records (Project → Goal → Task → Subtask → Execution → Evidence, with
// Decisions), states and blocking reasons come from PADOS statuses, the layout is deterministic, the view memory is
// immutable, and the rendered page shows every state (empty, map, list, detail) without inventing an acceptance.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { LocaleContext, translate } from "./i18n";
import {
  EMPTY_MEMORY, LAYOUT, allIds, buildWorkTree, defaultExpanded, edgePath, expandedSet, findNode, layoutTree, pathTo, searchNodes,
  selectNode, selectedNode, stateOf, toggleNode, visibleRows, withExpanded, type MapNode, type WorkMapData,
} from "./work-map";
import { WorkMapView, textOf } from "./work-map-page";

const P = "11111111-2222-4333-8444-555555555555";
const SHA = "a".repeat(64);

function fixture(): WorkMapData {
  return {
    project: { id: P, name: "文件檢查示範", folder: "demo-0a1b" },
    work_requests: [
      { id: "req-done", title: "檢查文件連結", status: "PENDING_WORKER_ROUTER", created_at: "2026-11-16T10:00:00Z", request_text: "請檢查連結", requested_capability: "RUN_STATIC_ANALYSIS" },
      { id: "req-wait", title: "等待核准的需求", status: "PENDING_WORKER_ROUTER", created_at: "2026-11-16T11:00:00Z", request_text: "稍後再做" },
    ],
    approvals: [
      { id: "ap-done", status: "CONSUMED", subject_kind: "WORK_ROUTE", subject_json: { request_id: "req-done" }, created_at: "2026-11-16T10:01:00Z", decided_at: "2026-11-16T10:02:00Z" },
      { id: "ap-wait", status: "PENDING", subject_kind: "WORK_ROUTE", subject_json: { request_id: "req-wait" }, created_at: "2026-11-16T11:01:00Z" },
      { id: "ap-result", status: "PENDING", subject_kind: "RESULT_ACCEPTANCE", subject_json: null },
    ],
    goals: [{ id: "goal-1", request_id: "req-done", approval_id: "ap-done", objective: "檢查文件連結: 請檢查連結", status: "ACTIVE" }],
    plans: [{ id: "plan-1", task_goal_id: "goal-1", approval_id: "ap-done", status: "ACTIVE", steps: [
      { id: "step-1", sequence: 1, title: "Check every Markdown link", required_capability: "RUN_STATIC_ANALYSIS", command_id: "markdown_reference_check", risk_level: "LOW", status: "PASSED" },
    ] }],
    loops: [
      { id: "loop-cancelled", status: "CANCELLED", request_id: "req-done", active_plan_id: "plan-1", approval_id: "ap-done", created_at: "2026-11-16T10:03:00Z" },
      { id: "loop-completed", status: "COMPLETED", request_id: "req-done", active_plan_id: "plan-1", approval_id: "ap-done", created_at: "2026-11-16T10:10:00Z", current_iteration: 1, max_iterations: 4 },
    ],
    candidates: [{ candidate_id: "cand-1", loop_id: "loop-completed", verification_status: "PASSED", approval_status: "PENDING", decision: null, created_at: "2026-11-16T10:20:00Z" }],
    execution: {
      iterations: [
        { id: "it-1", loop_id: "loop-cancelled", iteration_number: 1, step_id: "step-1", step_sequence: 1, job_id: "job-1", status: "DECIDED", decision: "WAIT_FOR_HUMAN", started_at: "2026-11-16T10:04:00Z" },
        { id: "it-2", loop_id: "loop-completed", iteration_number: 1, step_id: "step-1", step_sequence: 1, job_id: "job-2", status: "DECIDED", decision: "COMPLETE", started_at: "2026-11-16T10:11:00Z" },
      ],
      jobs: [
        { id: "job-1", status: "SUCCEEDED", worker_type: "DETERMINISTIC_TOOL", capability_id: "RUN_STATIC_ANALYSIS", exit_code: 1, validation_status: "PASSED", queued_at: "2026-11-16T10:04:00Z" },
        { id: "job-2", status: "SUCCEEDED", worker_type: "DETERMINISTIC_TOOL", capability_id: "RUN_STATIC_ANALYSIS", exit_code: 0, validation_status: "PASSED", queued_at: "2026-11-16T10:11:00Z", started_at: "2026-11-16T10:11:01Z", completed_at: "2026-11-16T10:11:02Z" },
      ],
      artifacts: [
        { job_id: "job-2", artifact_type: "stdout", sha256: SHA, created_at: "2026-11-16T10:11:02Z" },
        { job_id: "job-2", artifact_type: "stderr", sha256: "b".repeat(64), created_at: "2026-11-16T10:11:02Z" },
      ],
    },
    step_up: { remote_console: "NOT_CONFIGURED", presence_device: "UNKNOWN", approvers: 0 },
  };
}

const t = (key: Parameters<typeof translate>[1], params?: Parameters<typeof translate>[2]) => translate("en", key, params);
const kinds = (n: MapNode): string[] => n.children.map((c) => c.kind);

describe("work map tree", () => {
  const root = buildWorkTree(fixture());

  it("projects Project → Goal → Task → Subtask → Execution → Evidence with Decisions where they are made", () => {
    expect(root.kind).toBe("project");
    expect(root.children.map((g) => textOf(t, g.title))).toEqual(["等待核准的需求", "檢查文件連結"]);  // newest first
    const goal = findNode(root, "goal:req-done")!;
    expect(kinds(goal)).toEqual(["decision", "task"]);
    const task = findNode(root, "task:plan-1")!;
    expect(kinds(task)).toEqual(["subtask", "execution", "execution"]);
    expect(task.children.slice(1).map((c) => c.id)).toEqual(["execution:loop:loop-completed", "execution:loop:loop-cancelled"]);
    const step = findNode(root, "subtask:step-1")!;
    expect(step.children.map((c) => c.id)).toEqual(["execution:iteration:it-1", "execution:iteration:it-2"]);
    const run = findNode(root, "execution:iteration:it-2")!;
    expect(run.children.map((c) => [c.kind, textOf(t, c.title)])).toEqual([["evidence", "stdout"], ["evidence", "stderr"]]);
    expect(run.children[0].facts.find((f) => f.term === "map.fact.sha256")?.value).toBe(SHA);
    const decision = findNode(root, "decision:candidate:cand-1")!;
    expect(pathTo(root, decision.id)).toEqual([root.id, "goal:req-done", "task:plan-1", "execution:loop:loop-completed"]);
  });

  it("never shows a result as accepted without a recorded APPROVE, and names why it waits", () => {
    const decision = findNode(root, "decision:candidate:cand-1")!;
    expect(decision.status).toBe("WAITING_STEP_UP");
    expect(decision.state).toBe("waiting");
    expect(textOf(t, decision.blocking!)).toContain("Remote console is not configured");
    const goal = findNode(root, "goal:req-done")!;
    expect(goal.blocking).toEqual(decision.blocking);  // the goal surfaces the most urgent blocker below it
    const accepted = buildWorkTree({ ...fixture(), candidates: [{ ...fixture().candidates[0], decision: "APPROVE", approval_status: "APPROVED" }] });
    expect(findNode(accepted, "decision:candidate:cand-1")!.status).toBe("ACCEPTED");
    const failed = buildWorkTree({ ...fixture(), candidates: [{ ...fixture().candidates[0], verification_status: "FAILED" }] });
    expect(findNode(failed, "decision:candidate:cand-1")!.state).toBe("failed");
  });

  it("derives blocking reasons and next steps from PADOS statuses", () => {
    const waiting = findNode(root, "goal:req-wait")!;
    expect(waiting.status).toBe("PENDING_APPROVAL");
    expect(textOf(t, waiting.blocking!)).toBe("The plan waits for approval.");
    expect(textOf(t, waiting.next!)).toBe("Approve, modify or reject the plan.");
    expect(findNode(root, "execution:iteration:it-1")!.status).toBe("WAIT_FOR_HUMAN");
    const human = buildWorkTree({ ...fixture(), loops: [{ ...fixture().loops[0], status: "WAITING_HUMAN" }], candidates: [] });
    expect(textOf(t, findNode(human, "execution:loop:loop-cancelled")!.blocking!)).toBe("The check found problems and waits for you.");
    const fresh = buildWorkTree({ ...fixture(), work_requests: [{ id: "r", title: "new", status: "PENDING_WORKER_ROUTER" }], approvals: [], goals: [], plans: [], loops: [], candidates: [] });
    expect(textOf(t, findNode(fresh, "goal:r")!.next!)).toBe("Create a plan for this request.");
    const completedNoCandidate = buildWorkTree({ ...fixture(), candidates: [] });
    expect(textOf(t, findNode(completedNoCandidate, "execution:loop:loop-completed")!.next!)).toBe("Submit the result as a ResultCandidate.");
  });

  it("keeps dependencies and execution history", () => {
    const loop = findNode(root, "execution:loop:loop-completed")!;
    expect(loop.dependencies.map((d) => textOf(t, d))).toEqual(["Task Plan plan-1", "Approval ap-done (CONSUMED)"]);
    expect(loop.history.map((h) => h.status)).toEqual(["COMPLETE"]);
    expect(findNode(root, "goal:req-done")!.history.map((h) => h.status)).toEqual(["COMPLETED", "CANCELLED"]);
    expect(findNode(root, "execution:iteration:it-2")!.history.map((h) => textOf(t, h.title))).toEqual(["Queued", "Started", "Finished"]);
    expect(findNode(root, "goal:req-done")!.refs).toMatchObject({ projectId: P, loopId: "loop-completed", draftTitle: "檢查文件連結" });
  });

  it("maps every PADOS status to a display state", () => {
    expect(["COMPLETED", "APPROVED", "PASSED", "SUCCEEDED", "CONSUMED"].map(stateOf)).toEqual(Array(5).fill("done"));
    expect(["RUNNING", "QUEUED", "ACTIVE"].map(stateOf)).toEqual(["active", "active", "active"]);
    expect(["PENDING", "WAITING_HUMAN", "PAUSED", "PENDING_APPROVAL"].map(stateOf)).toEqual(Array(4).fill("waiting"));
    expect(["FAILED_TERMINAL", "REJECTED", "TIMED_OUT", "EXPIRED"].map(stateOf)).toEqual(Array(4).fill("failed"));
    expect(["BLOCKED", "REVISION_REQUESTED"].map(stateOf)).toEqual(["blocked", "blocked"]);
    expect(["CANCELLED", "SUPERSEDED"].map(stateOf)).toEqual(["stopped", "stopped"]);
    expect([undefined, "SOMETHING_NEW"].map(stateOf)).toEqual(["unknown", "unknown"]);
  });

  it("an empty project is a single node that asks for a work request", () => {
    const empty = buildWorkTree({ ...fixture(), work_requests: [], approvals: [], goals: [], plans: [], loops: [], candidates: [], execution: { iterations: [], jobs: [], artifacts: [] } });
    expect(empty.children).toEqual([]);
    expect(textOf(t, empty.next!)).toBe("Make a new work request.");
  });

  it("finds a task by its words", () => {
    expect(searchNodes(root, "檢查文件")).toContain("goal:req-done");
    expect(searchNodes(root, "stderr")).toEqual(["evidence:job-2:stderr:1"]);
    expect(searchNodes(root, "  ")).toEqual([]);
  });
});

describe("layout and view memory", () => {
  const root = buildWorkTree(fixture());

  it("lays out only expanded branches, left to right, parents centred on their children", () => {
    const collapsed = layoutTree(root, new Set());
    expect(collapsed.placed.map((p) => p.node.id)).toEqual([root.id, "goal:req-wait", "goal:req-done"]);
    const [top, first, second] = collapsed.placed;
    expect(first.x).toBe(LAYOUT.pad + LAYOUT.column);
    expect(top.y).toBe((first.y + second.y) / 2);
    expect(collapsed.edges).toHaveLength(2);
    const all = layoutTree(root, new Set(allIds(root)));
    expect(all.placed).toHaveLength(countNodes(root));
    expect(all.width).toBeGreaterThan(5 * LAYOUT.column);
    expect(edgePath(all.edges[0].from, all.edges[0].to)).toMatch(/^M\d+(\.\d+)?,\d+(\.\d+)? C/);
  });

  it("lists visible rows and remembers expansion and selection immutably, per project", () => {
    expect(defaultExpanded(root)).toEqual([root.id, "goal:req-wait", "goal:req-done"]);
    const before = EMPTY_MEMORY;
    const toggled = toggleNode(before, root, "goal:req-done");
    expect(before.expanded).toEqual({});
    expect(expandedSet(toggled, root).has("goal:req-done")).toBe(false);
    const selected = selectNode(toggled, root, "evidence:job-2:stdout:0");
    expect(selectedNode(selected, root)?.id).toBe("evidence:job-2:stdout:0");
    const open = expandedSet(selected, root);
    for (const id of pathTo(root, "evidence:job-2:stdout:0")) expect(open.has(id)).toBe(true);
    expect(visibleRows(root, open).some((row) => row.node.id === "evidence:job-2:stdout:0")).toBe(true);
    expect(visibleRows(root, new Set()).map((row) => row.depth)).toEqual([0, 1, 1]);
    const reread = buildWorkTree({ ...fixture(), execution: { ...fixture().execution, artifacts: [] } });
    expect(selectedNode(selected, reread)).toBeNull();  // the node is gone from the backend: no stale selection
    expect(withExpanded(EMPTY_MEMORY, "other", ["x"]).expanded).toEqual({ other: ["x"] });
  });
});

function countNodes(node: MapNode): number {
  return 1 + node.children.reduce((sum, child) => sum + countNodes(child), 0);
}

function render(data: WorkMapData, memory = EMPTY_MEMORY, locale: "zh-TW" | "en" = "zh-TW"): string {
  return renderToStaticMarkup(createElement(LocaleContext.Provider, { value: locale },
    createElement(WorkMapView, { data, memory, onMemory: () => undefined, onOpenWork: () => undefined })));
}

describe("work map page states", () => {
  it("renders the mind map with SVG edges and a detail hint", () => {
    const html = render(fixture());
    expect(html).toContain('data-testid="work-map-canvas"');
    expect(html).toContain("<svg");
    expect(html).toContain("<path");
    expect(html).toContain("檢查文件連結");
    expect(html).toContain("選擇一個節點");
    expect(html).not.toContain('data-testid="work-map-list"');
  });

  it("renders the list view and the selected node's details", () => {
    const root = buildWorkTree(fixture());
    const memory = { ...selectNode(EMPTY_MEMORY, root, "decision:candidate:cand-1"), mode: "list" as const };
    const html = render(fixture(), memory);
    expect(html).toContain('data-testid="work-map-list"');
    expect(html).toContain('role="tree"');
    expect(html).toContain("WAITING_STEP_UP");
    expect(html).toContain("尚未設定 Remote 主控台");
    expect(html).toContain("在 Remote 主控台決策");
    expect(html).not.toContain("ACCEPTED");
  });

  it("an empty project offers a work request, in either language", () => {
    const empty = { ...fixture(), work_requests: [], approvals: [], goals: [], plans: [], loops: [], candidates: [], execution: { iterations: [], jobs: [], artifacts: [] } };
    expect(render(empty)).toContain("這個專案還沒有任何工作");
    expect(render(empty, EMPTY_MEMORY, "en")).toContain("New work request from here");
  });
});
