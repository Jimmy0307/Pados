// SPDX-License-Identifier: AGPL-3.0-only
// 工作心智圖 (Work Map): pure logic. A tree projected from what GET /control/projects/{id}/work-map returns (the existing
// PADOS records: work requests, WORK_ROUTE approvals, task goals, plans and steps, TaskLoops and iterations, worker jobs,
// artifact receipts, ResultCandidates and their decisions). Nothing here stores, edits or decides anything: the tree is
// rebuilt from the backend on every read, and the only things kept in page memory are which nodes are expanded, which one
// is selected and the view mode. The node kinds follow the work graph vocabulary (pados.work_protocol WorkNodeKind):
// project=PROJECT, goal=GOAL, task=TASK, subtask=PLAN_STEP, execution=TaskLoop/iteration/job, evidence=ARTIFACT/RECEIPT,
// decision=DECISION.
import type { MessageKey, Params } from "./i18n";
import { candidateState, loopPhase, type CandidateRow, type StepUp } from "./product-flow";

export type NodeKind = "project" | "goal" | "task" | "subtask" | "execution" | "evidence" | "decision";
export type NodeState = "done" | "active" | "waiting" | "blocked" | "failed" | "stopped" | "unknown";
export type Text = { text: string } | { key: MessageKey; params?: Params };
export type HistoryEntry = { title: Text; status: string; at?: string | null };
export type Fact = { term: MessageKey; value: string };
export type MapNode = {
  id: string; kind: NodeKind; title: Text; status: string; state: NodeState;
  blocking: Text | null; next: Text | null; dependencies: Text[]; history: HistoryEntry[]; facts: Fact[];
  search: string; refs: { projectId: string; loopId?: string; draftTitle?: string; draftText?: string };
  children: MapNode[];
};

export type WorkMapApproval = { id: string; status: string; version?: number | string; subject_kind?: string | null; subject_json?: { request_id?: string; kind?: string } | null; proposal_summary?: string | null; decision_reason?: string | null; created_at?: string; decided_at?: string | null; expires_at?: string | null };
export type WorkMapRequest = { id: string; title: string; status: string; created_at?: string; request_text?: string | null; requested_capability?: string | null; root_goal_id?: string | null };
export type WorkMapGoal = { id: string; request_id: string; approval_id?: string | null; objective: string; status: string; risk_level?: string; created_at?: string };
export type WorkMapStep = { id: string; sequence: number; title: string; step_type?: string; required_capability?: string; command_id?: string | null; risk_level?: string; status: string };
export type WorkMapPlan = { id: string; task_goal_id: string; approval_id?: string | null; status: string; created_at?: string; steps: WorkMapStep[] };
export type WorkMapLoop = { id: string; status: string; request_id?: string; task_goal_id?: string; active_plan_id?: string | null; approval_id?: string; current_iteration?: number; max_iterations?: number; created_at?: string; started_at?: string | null; completed_at?: string | null };
export type WorkMapCandidate = CandidateRow & { candidate_id: string; loop_id: string; request_id?: string; approval_id?: string | null; created_at?: string; decided_at?: string | null };
export type WorkMapIteration = { id: string; loop_id: string; iteration_number: number; plan_id?: string; step_id?: string; step_sequence?: number | null; job_id?: string | null; status: string; decision?: string | null; decision_reason?: string | null; started_at?: string | null; completed_at?: string | null };
export type WorkMapJob = { id: string; status: string; capability_id?: string; worker_type?: string; exit_code?: number | null; validation_status?: string; error_code?: string | null; queued_at?: string | null; started_at?: string | null; completed_at?: string | null };
export type WorkMapArtifact = { job_id: string; artifact_type: string; sha256: string; created_at?: string };
export type WorkMapData = {
  project: { id: string; name: string; status?: string; folder?: string };
  work_requests: WorkMapRequest[]; approvals: WorkMapApproval[]; goals: WorkMapGoal[]; plans: WorkMapPlan[];
  loops: WorkMapLoop[]; candidates: WorkMapCandidate[];
  execution: { iterations: WorkMapIteration[]; jobs: WorkMapJob[]; artifacts: WorkMapArtifact[]; truncated?: boolean };
  step_up?: StepUp;
};

const short = (value: string | null | undefined): string => {
  const text = String(value || "");
  return text.length > 12 ? text.slice(0, 8) : text;
};
const raw = (text: string): Text => ({ text });
const msg = (key: MessageKey, params?: Params): Text => (params ? { key, params } : { key });

/** The display state of any PADOS status (a loop, a step, a job, an approval, a goal, a candidate). */
export function stateOf(status: string | null | undefined): NodeState {
  const value = (status || "").toUpperCase();
  if (!value) return "unknown";
  if (/^(CANCELLED|SUPERSEDED|SKIPPED|CANCEL)$/.test(value)) return "stopped";
  if (/FAIL|REJECT|TIMED_OUT|EXPIRED/.test(value)) return "failed";
  if (/^(BLOCKED|BLOCK|REVISION_REQUESTED|AT_RISK)$/.test(value)) return "blocked";
  if (/^(COMPLETED|COMPLETE|APPROVED|APPROVE|PASSED|SUCCEEDED|ACHIEVED|ACCEPTED|CONSUMED|DECIDED|ADVANCE_STEP|VERIFIED)$/.test(value)) return "done";
  if (/^(RUNNING|VALIDATING|ROUTING|ROUTED|QUEUED|ACTIVE|CREATED|CONTINUE|RETRY_SAME_STEP)$/.test(value)) return "active";
  if (/WAIT|PENDING|PAUSE|READY|DRAFT|PROPOSED|PARTIAL/.test(value)) return "waiting";
  return "unknown";
}

function node(id: string, kind: NodeKind, title: Text, status: string, projectId: string, rest: Partial<MapNode> = {}): MapNode {
  const titleText = "text" in title ? title.text : Object.values(title.params || {}).join(" ");
  return {
    id, kind, title, status, state: stateOf(status), blocking: null, next: null, dependencies: [], history: [], facts: [],
    search: `${titleText} ${status} ${id}`.toLowerCase(), refs: { projectId }, children: [], ...rest,
  };
}

function group<T>(items: T[], key: (item: T) => string | null | undefined): Map<string, T[]> {
  const out = new Map<string, T[]>();
  for (const item of items) {
    const value = key(item);
    if (!value) continue;
    out.set(value, [...(out.get(value) || []), item]);
  }
  return out;
}

const byTime = <T extends { created_at?: string | null }>(a: T, b: T): number => String(b.created_at || "").localeCompare(String(a.created_at || ""));

type Index = {
  projectId: string; approvalById: Map<string, WorkMapApproval>; routeByRequest: Map<string, WorkMapApproval[]>;
  goalsByRequest: Map<string, WorkMapGoal[]>; plansByGoal: Map<string, WorkMapPlan[]>; loopsByPlan: Map<string, WorkMapLoop[]>;
  loopsByRequest: Map<string, WorkMapLoop[]>; iterationsByStep: Map<string, WorkMapIteration[]>;
  iterationsByLoop: Map<string, WorkMapIteration[]>; jobById: Map<string, WorkMapJob>; artifactsByJob: Map<string, WorkMapArtifact[]>;
  candidatesByLoop: Map<string, WorkMapCandidate[]>; planIds: Set<string>;
};

function indexOf(data: WorkMapData): Index {
  const routes = data.approvals.filter((a) => (a.subject_kind || a.subject_json?.kind) === "WORK_ROUTE");
  return {
    projectId: data.project.id,
    approvalById: new Map(data.approvals.map((a) => [a.id, a])),
    routeByRequest: group(routes, (a) => a.subject_json?.request_id),
    goalsByRequest: group(data.goals, (g) => g.request_id),
    plansByGoal: group(data.plans, (p) => p.task_goal_id),
    loopsByPlan: group(data.loops, (l) => l.active_plan_id),
    loopsByRequest: group(data.loops, (l) => l.request_id),
    iterationsByStep: group(data.execution.iterations, (i) => i.step_id),
    iterationsByLoop: group(data.execution.iterations, (i) => i.loop_id),
    jobById: new Map(data.execution.jobs.map((j) => [j.id, j])),
    artifactsByJob: group(data.execution.artifacts, (a) => a.job_id),
    candidatesByLoop: group(data.candidates, (c) => c.loop_id),
    planIds: new Set(data.plans.map((p) => p.id)),
  };
}

function evidenceNode(ix: Index, artifact: WorkMapArtifact, index: number): MapNode {
  return node(`evidence:${artifact.job_id}:${artifact.artifact_type}:${index}`, "evidence", raw(artifact.artifact_type), "RECORDED", ix.projectId, {
    state: "done",
    facts: [{ term: "map.fact.sha256", value: artifact.sha256 }, { term: "map.fact.job", value: artifact.job_id },
      { term: "map.fact.recordedAt", value: artifact.created_at || "—" }],
    dependencies: [msg("map.dep.job", { id: short(artifact.job_id) })],
    search: `${artifact.artifact_type} ${artifact.sha256} evidence`.toLowerCase(),
  });
}

function iterationNode(ix: Index, iteration: WorkMapIteration): MapNode {
  const job = iteration.job_id ? ix.jobById.get(iteration.job_id) : undefined;
  const artifacts = (iteration.job_id && ix.artifactsByJob.get(iteration.job_id)) || [];
  const status = iteration.decision || job?.status || iteration.status;
  const facts: Fact[] = [{ term: "map.fact.iterationStatus", value: iteration.status }];
  if (iteration.decision) facts.push({ term: "map.fact.decision", value: `${iteration.decision}${iteration.decision_reason ? ` · ${iteration.decision_reason}` : ""}` });
  if (job) {
    facts.push({ term: "map.fact.job", value: `${job.id} · ${job.status}` }, { term: "map.fact.worker", value: `${job.worker_type || "—"} · ${job.capability_id || "—"}` },
      { term: "map.fact.validation", value: `${job.validation_status || "—"}${job.exit_code === null || job.exit_code === undefined ? "" : ` · exit ${job.exit_code}`}${job.error_code ? ` · ${job.error_code}` : ""}` });
  }
  return node(`execution:iteration:${iteration.id}`, "execution", msg("map.iteration", { n: iteration.iteration_number, loop: short(iteration.loop_id) }), status, ix.projectId, {
    refs: { projectId: ix.projectId, loopId: iteration.loop_id },
    facts,
    dependencies: [msg("map.dep.step", { n: iteration.step_sequence ?? "?" }), msg("map.dep.loop", { id: short(iteration.loop_id) })],
    history: job ? [{ title: msg("map.hist.queued"), status: job.status, at: job.queued_at }, { title: msg("map.hist.started"), status: job.status, at: job.started_at },
      { title: msg("map.hist.finished"), status: job.status, at: job.completed_at }].filter((h) => h.at) : [{ title: msg("map.hist.started"), status: iteration.status, at: iteration.started_at }],
    next: artifacts.length === 0 && stateOf(status) === "done" ? msg("map.next.noEvidence") : null,
    children: artifacts.map((artifact, index) => evidenceNode(ix, artifact, index)),
  });
}

function candidateNode(ix: Index, candidate: WorkMapCandidate, stepUp: StepUp | undefined): MapNode {
  const state = candidateState(candidate);
  const status = state === "WAITING_STEP_UP" ? "WAITING_STEP_UP" : state || "UNKNOWN";
  const waiting = state === "WAITING_STEP_UP";
  return node(`decision:candidate:${candidate.candidate_id}`, "decision", msg("map.candidate"), status, ix.projectId, {
    refs: { projectId: ix.projectId, loopId: candidate.loop_id },
    blocking: waiting ? msg(stepUp?.remote_console === "CONFIGURED" ? "map.block.stepUp" : "map.block.stepUpNotConfigured") : state === "VERIFICATION_FAILED" ? msg("map.block.verification") : null,
    next: waiting ? msg("map.next.stepUp") : null,
    facts: [{ term: "map.fact.candidate", value: candidate.candidate_id }, { term: "map.fact.verification", value: candidate.verification_status || "—" },
      { term: "map.fact.approval", value: candidate.approval_status || "—" }, { term: "map.fact.decision", value: candidate.decision || "—" },
      { term: "map.fact.decidedAt", value: candidate.decided_at || "—" }],
    dependencies: [msg("map.dep.loop", { id: short(candidate.loop_id) })],
    history: [{ title: msg("map.hist.submitted"), status: candidate.verification_status || "—", at: candidate.created_at },
      ...(candidate.decided_at ? [{ title: msg("map.hist.decided"), status: candidate.decision || "—", at: candidate.decided_at }] : [])],
    search: `${candidate.candidate_id} ${status} decision candidate`.toLowerCase(),
  });
}

function loopBlocking(loop: WorkMapLoop, candidates: WorkMapCandidate[]): { blocking: Text | null; next: Text | null } {
  switch (loopPhase(loop.status)) {
    case "waiting_human": return { blocking: msg("map.block.waitingHuman"), next: msg("map.next.fixAndRecheck") };
    case "paused": return { blocking: msg("map.block.paused"), next: msg("map.next.resume") };
    case "blocked": return { blocking: msg("map.block.blocked"), next: msg("map.next.newRequest") };
    case "failed": return { blocking: msg("map.block.failed", { status: loop.status }), next: msg("map.next.newRequest") };
    case "running": return { blocking: null, next: msg("map.next.running") };
    case "completed": return { blocking: null, next: candidates.length === 0 ? msg("map.next.submit") : null };
    default: return { blocking: null, next: null };
  }
}

function loopNode(ix: Index, loop: WorkMapLoop, stepUp: StepUp | undefined): MapNode {
  const candidates = [...(ix.candidatesByLoop.get(loop.id) || [])].sort(byTime);
  const iterations = [...(ix.iterationsByLoop.get(loop.id) || [])].sort((a, b) => a.iteration_number - b.iteration_number);
  const approval = loop.approval_id ? ix.approvalById.get(loop.approval_id) : undefined;
  const children = candidates.map((c) => candidateNode(ix, c, stepUp));
  const { blocking, next } = loopBlocking(loop, candidates);
  return node(`execution:loop:${loop.id}`, "execution", msg("map.loop", { id: short(loop.id) }), loop.status, ix.projectId, {
    refs: { projectId: ix.projectId, loopId: loop.id },
    blocking: blocking || firstBlocking(children), next: next || firstNext(children),
    facts: [{ term: "map.fact.loop", value: loop.id }, { term: "map.fact.progress", value: `${loop.current_iteration ?? 0} / ${loop.max_iterations ?? "—"}` },
      { term: "map.fact.startedAt", value: loop.started_at || "—" }, { term: "map.fact.completedAt", value: loop.completed_at || "—" }],
    dependencies: [...(loop.active_plan_id ? [msg("map.dep.plan", { id: short(loop.active_plan_id) })] : []),
      ...(approval ? [msg("map.dep.approval", { id: short(approval.id), status: approval.status })] : [])],
    history: iterations.map((i) => ({ title: msg("map.iteration", { n: i.iteration_number, loop: short(loop.id) }), status: i.decision || i.status, at: i.started_at })),
    children,
  });
}

function stepNode(ix: Index, step: WorkMapStep, previous: WorkMapStep | undefined): MapNode {
  const iterations = [...(ix.iterationsByStep.get(step.id) || [])].sort((a, b) => String(a.started_at || "").localeCompare(String(b.started_at || "")) || a.iteration_number - b.iteration_number);
  const children = iterations.map((i) => iterationNode(ix, i));
  return node(`subtask:${step.id}`, "subtask", msg("map.step", { n: step.sequence, title: step.title }), step.status, ix.projectId, {
    facts: [{ term: "map.fact.capability", value: `${step.required_capability || "—"}${step.command_id ? ` · ${step.command_id}` : ""}` },
      { term: "map.fact.risk", value: step.risk_level || "—" }, { term: "map.fact.stepType", value: step.step_type || "—" }],
    dependencies: previous ? [msg("map.dep.step", { n: previous.sequence })] : [],
    history: iterations.map((i) => ({ title: msg("map.iteration", { n: i.iteration_number, loop: short(i.loop_id) }), status: i.decision || i.status, at: i.started_at })),
    next: children.length === 0 ? msg("map.next.notRunYet") : firstNext(children),
    children,
  });
}

function planNode(ix: Index, plan: WorkMapPlan, goal: WorkMapGoal, stepUp: StepUp | undefined): MapNode {
  const steps = [...plan.steps].sort((a, b) => a.sequence - b.sequence);
  const loops = [...(ix.loopsByPlan.get(plan.id) || [])].sort(byTime);
  const children = [...steps.map((step, i) => stepNode(ix, step, steps[i - 1])), ...loops.map((loop) => loopNode(ix, loop, stepUp))];
  const approval = plan.approval_id ? ix.approvalById.get(plan.approval_id) : undefined;
  const latest = loops[0];
  return node(`task:${plan.id}`, "task", msg("map.task", { steps: steps.length, id: short(plan.id) }), latest?.status || plan.status, ix.projectId, {
    refs: { projectId: ix.projectId, loopId: latest?.id, draftTitle: goal.objective.slice(0, 120), draftText: goal.objective },
    blocking: firstBlocking(children.filter((c) => c.id.startsWith("execution:loop:")).slice(0, 1)),
    next: latest ? firstNext(children.filter((c) => c.id === `execution:loop:${latest.id}`)) : msg("map.next.startRun"),
    facts: [{ term: "map.fact.plan", value: `${plan.id} · ${plan.status}` }, { term: "map.fact.objective", value: goal.objective }],
    dependencies: [msg("map.dep.goal", { id: short(goal.id) }), ...(approval ? [msg("map.dep.approval", { id: short(approval.id), status: approval.status })] : [])],
    history: loops.map((loop) => ({ title: msg("map.loop", { id: short(loop.id) }), status: loop.status, at: loop.created_at })),
    children,
  });
}

function approvalNode(ix: Index, approval: WorkMapApproval): MapNode {
  const pending = approval.status === "PENDING";
  return node(`decision:approval:${approval.id}`, "decision", msg("map.planApproval"), approval.status, ix.projectId, {
    blocking: pending ? msg("map.block.planApproval") : approval.status === "REVISION_REQUESTED" ? msg("map.block.revision") : null,
    next: pending ? msg("map.next.approvePlan") : null,
    facts: [{ term: "map.fact.approval", value: `${approval.id} · ${approval.status}` }, { term: "map.fact.summary", value: approval.proposal_summary || "—" },
      { term: "map.fact.reason", value: approval.decision_reason || "—" }, { term: "map.fact.expiresAt", value: approval.expires_at || "—" }],
    history: [{ title: msg("map.hist.proposed"), status: "PENDING", at: approval.created_at },
      ...(approval.decided_at ? [{ title: msg("map.hist.decided"), status: approval.status, at: approval.decided_at }] : [])],
  });
}

function requestNode(ix: Index, request: WorkMapRequest, stepUp: StepUp | undefined): MapNode {
  const goals = [...(ix.goalsByRequest.get(request.id) || [])].sort(byTime);
  const approvals = [...(ix.routeByRequest.get(request.id) || [])].sort(byTime);
  const plans = goals.flatMap((goal) => [...(ix.plansByGoal.get(goal.id) || [])].sort(byTime).map((plan) => ({ plan, goal })));
  const orphanLoops = (ix.loopsByRequest.get(request.id) || []).filter((loop) => !loop.active_plan_id || !ix.planIds.has(loop.active_plan_id));
  const children = [...approvals.map((a) => approvalNode(ix, a)), ...plans.map(({ plan, goal }) => planNode(ix, plan, goal, stepUp)),
    ...orphanLoops.map((loop) => loopNode(ix, loop, stepUp))];
  const goal = goals[0];
  const route = approvals[0];
  const loops = [...(ix.loopsByRequest.get(request.id) || [])].sort(byTime);
  const status = loops[0]?.status || goal?.status || (route ? (route.status === "PENDING" ? "PENDING_APPROVAL" : route.status) : request.status);
  const ownNext = !route ? msg("map.next.makePlan") : route.status === "APPROVED" && plans.length === 0 ? msg("map.next.startRun") : null;
  return node(`goal:${request.id}`, "goal", raw(request.title), status, ix.projectId, {
    refs: { projectId: ix.projectId, loopId: loops[0]?.id, draftTitle: request.title, draftText: request.request_text || "" },
    blocking: firstBlocking(children), next: firstNext(children) || ownNext,
    facts: [{ term: "map.fact.request", value: request.request_text || "—" }, { term: "map.fact.capability", value: request.requested_capability || "—" },
      ...(goal ? [{ term: "map.fact.objective" as MessageKey, value: `${goal.objective} · ${goal.status}` }] : []),
      { term: "map.fact.requestStatus", value: request.status }],
    history: loops.map((loop) => ({ title: msg("map.loop", { id: short(loop.id) }), status: loop.status, at: loop.created_at })),
    search: `${request.title} ${request.request_text || ""} ${status} ${request.id}`.toLowerCase(),
    children,
  });
}

/** The first blocking reason in a subtree, depth first in display order (newest work first). */
export function firstBlocking(nodes: MapNode[]): Text | null {
  for (const item of nodes) {
    if (item.blocking) return item.blocking;
    const inner = firstBlocking(item.children);
    if (inner) return inner;
  }
  return null;
}

function firstNext(nodes: MapNode[]): Text | null {
  for (const item of nodes) {
    if (item.next) return item.next;
  }
  return null;
}

/** The whole tree: Project → Goal → Task → Subtask → Execution → Evidence, with Decisions where they are made. */
export function buildWorkTree(data: WorkMapData): MapNode {
  const ix = indexOf(data);
  const requests = [...data.work_requests].sort(byTime);
  const goals = requests.map((request) => requestNode(ix, request, data.step_up));
  const shownLoops = new Set(data.loops.filter((loop) => (loop.request_id && requests.some((r) => r.id === loop.request_id))).map((l) => l.id));
  const strayCandidates = data.candidates.filter((c) => !shownLoops.has(c.loop_id)).map((c) => candidateNode(ix, c, data.step_up));
  const children = [...goals, ...strayCandidates];
  return node(`project:${data.project.id}`, "project", raw(data.project.name), data.project.status || "ACTIVE", data.project.id, {
    blocking: firstBlocking(children),
    next: children.length === 0 ? msg("map.next.newRequest") : null,
    facts: [{ term: "map.fact.folder", value: data.project.folder || "—" }, { term: "map.fact.goals", value: String(goals.length) },
      ...(data.execution.truncated ? [{ term: "map.fact.truncated" as MessageKey, value: "true" }] : [])],
    refs: { projectId: data.project.id },
    children,
  });
}

// --- tree navigation and layout ------------------------------------------------------------------------------------
export function walk(root: MapNode, visit: (item: MapNode, depth: number, parent: MapNode | null) => void, depth = 0, parent: MapNode | null = null): void {
  visit(root, depth, parent);
  for (const child of root.children) walk(child, visit, depth + 1, root);
}

export function findNode(root: MapNode, id: string | null | undefined): MapNode | null {
  let found: MapNode | null = null;
  if (id) walk(root, (item) => { if (!found && item.id === id) found = item; });
  return found;
}

/** Ids of the ancestors of ``id`` (root first), or [] when it is not in the tree. */
export function pathTo(root: MapNode, id: string): string[] {
  if (root.id === id) return [];
  for (const child of root.children) {
    if (child.id === id) return [root.id];
    const inner = pathTo(child, id);
    if (inner.length) return [root.id, ...inner];
  }
  return [];
}

/** Open by default: the project and its goals (two levels). */
export function defaultExpanded(root: MapNode): string[] {
  return [root.id, ...root.children.map((child) => child.id)];
}

export function allIds(root: MapNode): string[] {
  const ids: string[] = [];
  walk(root, (item) => { if (item.children.length) ids.push(item.id); });
  return ids;
}

export function searchNodes(root: MapNode, query: string): string[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [];
  const hits: string[] = [];
  walk(root, (item) => { if (item.search.includes(needle)) hits.push(item.id); });
  return hits;
}

export type VisibleRow = { node: MapNode; depth: number; expanded: boolean };

/** The rows of the list view: every node whose ancestors are all expanded (the root always is). */
export function visibleRows(root: MapNode, expanded: ReadonlySet<string>): VisibleRow[] {
  const rows: VisibleRow[] = [];
  const visit = (item: MapNode, depth: number) => {
    const open = depth === 0 || expanded.has(item.id);
    rows.push({ node: item, depth, expanded: open && item.children.length > 0 });
    if (open) item.children.forEach((child) => visit(child, depth + 1));
  };
  visit(root, 0);
  return rows;
}

export type Placed = { node: MapNode; x: number; y: number; depth: number; expanded: boolean };
export type Layout = { placed: Placed[]; edges: Array<{ from: Placed; to: Placed }>; width: number; height: number };
export const LAYOUT = { column: 250, row: 64, nodeWidth: 210, nodeHeight: 48, pad: 16 } as const;

/** A left-to-right tree: leaves on successive rows, each parent centred on its visible children. */
export function layoutTree(root: MapNode, expanded: ReadonlySet<string>): Layout {
  const placed: Placed[] = [];
  const edges: Layout["edges"] = [];
  let leaf = 0;
  const place = (item: MapNode, depth: number): Placed => {
    const open = (depth === 0 || expanded.has(item.id)) && item.children.length > 0;
    const kids = open ? item.children.map((child) => place(child, depth + 1)) : [];
    const y = kids.length ? (kids[0].y + kids[kids.length - 1].y) / 2 : LAYOUT.pad + (leaf++) * LAYOUT.row;
    const here: Placed = { node: item, x: LAYOUT.pad + depth * LAYOUT.column, y, depth, expanded: open };
    placed.push(here);
    kids.forEach((kid) => edges.push({ from: here, to: kid }));
    return here;
  };
  place(root, 0);
  const width = Math.max(...placed.map((p) => p.x)) + LAYOUT.nodeWidth + LAYOUT.pad;
  const height = Math.max(...placed.map((p) => p.y)) + LAYOUT.nodeHeight + LAYOUT.pad;
  return { placed: [...placed].sort((a, b) => a.depth - b.depth || a.y - b.y), edges, width, height };
}

/** The SVG path of an edge: a horizontal S-curve from the right side of the parent to the left side of the child. */
export function edgePath(from: Placed, to: Placed): string {
  const x1 = from.x + LAYOUT.nodeWidth;
  const y1 = from.y + LAYOUT.nodeHeight / 2;
  const x2 = to.x;
  const y2 = to.y + LAYOUT.nodeHeight / 2;
  const mid = (x1 + x2) / 2;
  return `M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}`;
}

// --- per-session view memory (page memory only: no browser storage; data is always re-read) --------------------------
export type MapMode = "map" | "list";
export type MapMemory = { projectId: string | null; mode: MapMode; expanded: Record<string, string[]>; selected: Record<string, string | null> };
export const EMPTY_MEMORY: MapMemory = { projectId: null, mode: "map", expanded: {}, selected: {} };

export function expandedSet(memory: MapMemory, root: MapNode): Set<string> {
  return new Set(memory.expanded[root.refs.projectId] ?? defaultExpanded(root));
}

export function withExpanded(memory: MapMemory, projectId: string, ids: Iterable<string>): MapMemory {
  return { ...memory, expanded: { ...memory.expanded, [projectId]: [...new Set(ids)] } };
}

export function toggleNode(memory: MapMemory, root: MapNode, id: string): MapMemory {
  const current = expandedSet(memory, root);
  const next = current.has(id) ? [...current].filter((value) => value !== id) : [...current, id];
  return withExpanded(memory, root.refs.projectId, next);
}

/** Select a node and open the path to it (a search hit or a deep link stays visible). */
export function selectNode(memory: MapMemory, root: MapNode, id: string | null): MapMemory {
  const projectId = root.refs.projectId;
  const opened = id ? withExpanded(memory, projectId, [...expandedSet(memory, root), ...pathTo(root, id)]) : memory;
  return { ...opened, selected: { ...opened.selected, [projectId]: id } };
}

/** The selection when it still exists after a re-read; otherwise none (the node is gone from the backend). */
export function selectedNode(memory: MapMemory, root: MapNode): MapNode | null {
  return findNode(root, memory.selected[root.refs.projectId]);
}
