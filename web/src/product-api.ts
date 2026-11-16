// SPDX-License-Identifier: AGPL-3.0-only
// The product work loop's calls: every one is an existing /control route (or one of the governed product routes added
// for it), sent through controlRequest with this browser's paired session or the control token. Nothing is mocked and
// nothing is decided here: the server decides, and its refusals come back as HttpError with their closed code.
import { controlRequest } from "./control-actions";
import type { ControlAuth } from "./control-auth";
import { MAX_ITERATIONS, newKey, type CandidateRow, type PlanTemplate, type StepUp } from "./product-flow";

type Fetcher = typeof fetch | undefined;
const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) });
const id = (value: string) => encodeURIComponent(value);

export type ProductProject = { id: string; name: string; status?: string; root_path?: string; updated_at?: string; folder?: string; description?: string };
export type DocumentEntry = { path: string; size: number; sha256: string | null; modified_at?: string };
export type DocumentProblem = { path: string; code: string };
/** The server's read-only write-recovery status: open intents, last writes that failed, edits made outside PADOS. */
export type DocumentReconciliation = { state: "CLEAN" | "PENDING" | "ATTENTION"; pending: number; failed: DocumentProblem[]; unaudited: DocumentProblem[] };
export type DocumentReceipt = { path: string; size: number; sha256: string; replaced: boolean; previous_sha256: string | null; replayed: boolean };
export type WorkRequest = { id: string; title: string; body: string; status?: string };
export type Approval = { id: string; status: string; version: number | string; subject_kind?: string | null; proposal_summary?: string | null; expires_at?: string | null };
export type Loop = { id: string; status: string; current_iteration?: number; max_iterations?: number; final_result?: unknown; request_id?: string; approval_id?: string; active_plan_id?: string; task_goal_id?: string; created_at?: string; completed_at?: string | null };
export type LoopEvent = { sequence_number?: number; event_type: string; event_time?: string; payload_json?: unknown };
export type Iteration = { id: string; iteration_number: number; job_id?: string | null; status: string; decision?: string | null; decision_reason?: string | null };
export type JobArtifact = { artifact_type: string; path?: string; sha256: string; created_at?: string };
export type ArtifactText = { artifact_type: string; sha256: string; integrity: string; content: string; redacted?: boolean };
export type Candidate = { candidate_id: string; verification_status: string; decision_status: string; approval_ref: string | null; loop_id: string };
export type ProjectWork = {
  project: ProductProject;
  work_requests: Array<{ id: string; title: string; status: string; created_at?: string; request_text?: string; requested_capability?: string | null }>;
  approvals: Approval[];
  goals: Array<{ id: string; objective: string; status: string; request_id: string; created_at?: string }>;
  plans: Array<{ id: string; task_goal_id: string; status: string; steps: Array<{ sequence: number; title: string; required_capability: string; command_id?: string | null; risk_level: string; status: string }> }>;
  loops: Loop[];
  candidates: Array<CandidateRow & { created_at?: string; approval_id?: string | null; decided_at?: string | null }>;
  events: Array<{ sequence_number: number; event_type: string; event_time?: string; payload_json?: unknown }>;
  step_up: StepUp;
};

export const listProjects = (auth: ControlAuth, f?: Fetcher) =>
  controlRequest<{ projects: ProductProject[] }>("/control/projects", auth, {}, f);

export const createProject = (auth: ControlAuth, name: string, description: string, key: string, f?: Fetcher) =>
  controlRequest<{ project: ProductProject; replayed: boolean; proof_scope: string }>("/control/projects", auth,
    json("POST", { name, description, idempotency_key: key }), f);

export const projectWork = (auth: ControlAuth, projectId: string, f?: Fetcher) =>
  controlRequest<ProjectWork>(`/control/projects/${id(projectId)}/work`, auth, {}, f);

export const contextSummary = (auth: ControlAuth, projectId: string, f?: Fetcher) =>
  controlRequest<{ context: { memory_counts: Array<{ verification_status: string; freshness_status: string; count: number }> } }>(
    `/control/projects/${id(projectId)}/context-summary`, auth, {}, f);

export const projectMessages = (auth: ControlAuth, projectId: string, f?: Fetcher) =>
  controlRequest<{ messages: unknown[] }>(`/control/projects/${id(projectId)}/messages`, auth, {}, f);

export const listDocuments = (auth: ControlAuth, projectId: string, f?: Fetcher) =>
  controlRequest<{ documents: DocumentEntry[]; writable: boolean; truncated: boolean; reconciliation?: DocumentReconciliation }>(`/control/projects/${id(projectId)}/documents`, auth, {}, f);

export const readDocument = (auth: ControlAuth, projectId: string, path: string, f?: Fetcher) =>
  controlRequest<{ document: { path: string; sha256: string; content: string } }>(
    `/control/projects/${id(projectId)}/documents/content?path=${encodeURIComponent(path)}`, auth, {}, f);

export const writeDocument = (auth: ControlAuth, projectId: string, input: { path: string; content: string; replace?: boolean; expectedSha256?: string | null }, key: string, f?: Fetcher) =>
  controlRequest<{ document: DocumentReceipt }>(`/control/projects/${id(projectId)}/documents`, auth, json("POST", {
    path: input.path, content: input.content, replace: Boolean(input.replace), expected_sha256: input.expectedSha256 ?? null, idempotency_key: key,
  }), f);

export const createCapturedRequest = (auth: ControlAuth, projectId: string, title: string, text: string, capability: string, key: string, f?: Fetcher) =>
  controlRequest<{ work_request: WorkRequest }>("/control/work-requests", auth, json("POST", {
    project_id: projectId, title, body: text, idempotency_key: key, requested_capability: capability,
  }), f);

export const proposePlan = (auth: ControlAuth, projectId: string, plan: PlanTemplate, key: string, f?: Fetcher) =>
  controlRequest<{ approval: Approval }>("/control/approvals", auth, json("POST", {
    project_id: projectId, proposal_summary: plan.proposalSummary, subject: plan.subject, idempotency_key: key,
  }), f);

export const getApproval = (auth: ControlAuth, approvalId: string, f?: Fetcher) =>
  controlRequest<{ approval: Approval }>(`/control/approvals/${id(approvalId)}`, auth, {}, f);

export const decide = (auth: ControlAuth, approval: Approval, verb: "approve" | "reject" | "revise", reason: string, f?: Fetcher) =>
  controlRequest<{ approval: Approval }>(`/control/approvals/${id(approval.id)}/${verb}`, auth, json("POST", {
    version: Number(approval.version), reason, idempotency_key: newKey(`web-${verb}`),
  }), f);

/** After 核准: the task goal, the plan and the TaskLoop, created and started through the existing routes. */
export async function startApprovedPlan(auth: ControlAuth, input: { projectId: string; requestId: string; approvalId: string; plan: PlanTemplate }, f?: Fetcher): Promise<Loop> {
  const goal = await controlRequest<{ task_goal: { id: string } }>("/control/task-goals", auth, json("POST", {
    project_id: input.projectId, request_id: input.requestId, approval_id: input.approvalId, idempotency_key: newKey("web-goal"),
    objective: input.plan.goal.objective, success_criteria: input.plan.goal.success_criteria,
  }), f);
  const plan = await controlRequest<{ task_plan: { id: string } }>("/control/task-plans", auth, json("POST", {
    task_goal_id: goal.task_goal.id, approval_id: input.approvalId, idempotency_key: newKey("web-taskplan"), steps: input.plan.steps,
  }), f);
  const loop = await controlRequest<{ loop: Loop }>("/control/loops", auth, json("POST", {
    task_goal_id: goal.task_goal.id, active_plan_id: plan.task_plan.id, approval_id: input.approvalId,
    max_iterations: MAX_ITERATIONS, idempotency_key: newKey("web-loop"),
  }), f);
  const started = await controlRequest<{ loop: Loop }>(`/control/loops/${id(loop.loop.id)}/start`, auth, json("POST", {}), f);
  return started.loop;
}

export const getLoop = (auth: ControlAuth, loopId: string, f?: Fetcher) =>
  controlRequest<{ loop: Loop }>(`/control/loops/${id(loopId)}`, auth, {}, f);

export const stepLoop = (auth: ControlAuth, loopId: string, f?: Fetcher) =>
  controlRequest<{ loop?: Loop; decision?: string; decision_reason?: string; status?: string }>(`/control/loops/${id(loopId)}/step`, auth,
    json("POST", { idempotency_key: newKey("web-step") }), f);

export const controlLoop = (auth: ControlAuth, loopId: string, verb: "pause" | "resume" | "cancel", f?: Fetcher) =>
  controlRequest<{ loop: Loop }>(`/control/loops/${id(loopId)}/${verb}`, auth, json("POST", {}), f);

export const loopEvents = (auth: ControlAuth, loopId: string, f?: Fetcher) =>
  controlRequest<{ events: LoopEvent[] }>(`/control/loops/${id(loopId)}/events`, auth, {}, f);

export const loopIterations = (auth: ControlAuth, loopId: string, f?: Fetcher) =>
  controlRequest<{ iterations: Iteration[] }>(`/control/loops/${id(loopId)}/iterations`, auth, {}, f);

export const jobArtifacts = (auth: ControlAuth, jobId: string, f?: Fetcher) =>
  controlRequest<{ artifacts: JobArtifact[] }>(`/control/jobs/${id(jobId)}/artifacts`, auth, {}, f);

export const artifactText = (auth: ControlAuth, jobId: string, type: "stdout" | "stderr", f?: Fetcher) =>
  controlRequest<{ artifact: ArtifactText }>(`/control/jobs/${id(jobId)}/artifacts/${type}/content`, auth, {}, f);

export const submitCandidate = (auth: ControlAuth, loopId: string, f?: Fetcher) =>
  controlRequest<{ candidate: Candidate }>(`/control/loops/${id(loopId)}/result-candidate`, auth, json("POST", {}), f);

export type Evidence = { jobId: string; artifacts: JobArtifact[]; stdout: ArtifactText | null; stderr: ArtifactText | null; iterations: Iteration[] };

/** The latest job of the loop, its recorded artifacts and the verified stdout/stderr texts (Evidence). */
export async function loadEvidence(auth: ControlAuth, loopId: string, f?: Fetcher): Promise<Evidence | null> {
  const { iterations } = await loopIterations(auth, loopId, f);
  const withJob = [...iterations].reverse().find((row) => row.job_id);
  if (!withJob?.job_id) return null;
  const jobId = withJob.job_id;
  const [{ artifacts }, stdout, stderr] = await Promise.all([
    jobArtifacts(auth, jobId, f),
    artifactText(auth, jobId, "stdout", f).then((r) => r.artifact).catch(() => null),
    artifactText(auth, jobId, "stderr", f).then((r) => r.artifact).catch(() => null),
  ]);
  return { jobId, artifacts, stdout, stderr, iterations };
}
