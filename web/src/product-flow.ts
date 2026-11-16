// SPDX-License-Identifier: AGPL-3.0-only
// Pure logic of the product work loop page: the deterministic plan template (no AI), reading the captured
// TaskEnvelope's root goal, the probe's evidence, the run phase, the candidate's decision state and plain-language
// failures. Nothing here decides anything: it shapes what the existing control routes accept and return. A result is
// shown as accepted only when the backend recorded an APPROVE decision for it (Model Output ≠ Truth; Execution Complete
// ≠ Semantic Acceptance).
import { HttpError } from "./http";
import { hasMessage, type MessageKey } from "./i18n";

export const CHECK_CAPABILITY = "RUN_STATIC_ANALYSIS";
export const CHECK_COMMAND = "markdown_reference_check";
export const OUTCOME_CONTRACT = "pados.capability-outcome.v1";
export const SUBJECT_SCHEMA = "pados.approval-subject.v1";
export const PLAN_SOURCE = "DETERMINISTIC_TEMPLATE";
export const MAX_ITERATIONS = 4;

export type CapabilityOption = { id: string; command: string | null; labelKey: MessageKey; available: boolean };

/** The runnable check, and what cannot run here (shown disabled, with the reason) because no AI is configured. */
export const CAPABILITY_OPTIONS: CapabilityOption[] = [
  { id: CHECK_CAPABILITY, command: CHECK_COMMAND, labelKey: "work.cap.check", available: true },
  { id: "SUMMARIZE", command: null, labelKey: "work.cap.summarize", available: false },
  { id: "MODIFY_MARKDOWN", command: null, labelKey: "work.cap.modify", available: false },
];

export const OPERATIONS = [{ step_sequence: 1, capability_id: CHECK_CAPABILITY, command_id: CHECK_COMMAND, max_risk_level: "LOW" }];

export type PlanTemplate = {
  source: typeof PLAN_SOURCE;
  risk: "LOW";
  subject: Record<string, unknown>;
  proposalSummary: string;
  goal: { objective: string; success_criteria: string[] };
  steps: Array<Record<string, unknown>>;
  allowedActions: string[];
  prohibitedActions: string[];
};

/** The one-step plan for the Markdown link check, bound to the captured envelope's root goal. */
export function buildPlanTemplate(input: { projectId: string; requestId: string; rootGoalId: string; title: string; text: string }): PlanTemplate {
  return {
    source: PLAN_SOURCE,
    risk: "LOW",
    subject: {
      kind: "WORK_ROUTE", schema_version: SUBJECT_SCHEMA, project_id: input.projectId, request_id: input.requestId,
      actor: { surfaces: ["TASK_LOOP"], worker_types: ["DETERMINISTIC_TOOL"] }, operations: OPERATIONS,
      scope_version_ref: null, task_envelope_hash: null,
    },
    proposalSummary: `Deterministic template (no AI): ${CHECK_COMMAND} on the project's Markdown. ${input.title}`.slice(0, 4000),
    goal: {
      objective: `${input.title}: ${input.text}`.slice(0, 1000),
      success_criteria: ["every relative Markdown link in the project resolves", "an execution receipt with artifact digests exists"],
    },
    steps: [{
      sequence: 1, product_goal_id: input.rootGoalId, title: "Check every Markdown link", step_type: "RUN_VALIDATION",
      required_capability: CHECK_CAPABILITY, command_id: CHECK_COMMAND, output_contract: { pados_capability_outcome: OUTCOME_CONTRACT },
    }],
    allowedActions: ["READ_REPOSITORY"],
    prohibitedActions: ["merge", "push", "deploy"],
  };
}

/** The root goal id of the TaskEnvelope the server captured in the work request body (null when there is none). */
export function rootGoalId(body: unknown): string | null {
  if (typeof body !== "string") return null;
  try {
    const parsed = JSON.parse(body) as { task_envelope?: { normalized_goals?: Array<{ goal_id?: unknown }> } };
    const goal = parsed.task_envelope?.normalized_goals?.[0]?.goal_id;
    return typeof goal === "string" && goal ? goal : null;
  } catch {
    return null;
  }
}

export type ProbeReport = { filesChecked: number | null; referencesChecked: number | null; broken: Array<{ file: string; target: string }>; error: string | null };

/** The link check's report (its stderr artifact): counts and broken links; an unreadable report is an error. */
export function parseProbeReport(text: string | null | undefined): ProbeReport | null {
  if (!text) return null;
  try {
    const value = JSON.parse(text) as Record<string, unknown>;
    const broken = Array.isArray(value.broken) ? value.broken.filter((item): item is { file: string; target: string } =>
      typeof item === "object" && item !== null && typeof (item as { file?: unknown }).file === "string"
      && typeof (item as { target?: unknown }).target === "string").map(({ file, target }) => ({ file, target })) : [];
    return {
      filesChecked: typeof value.files_checked === "number" ? value.files_checked : null,
      referencesChecked: typeof value.references_checked === "number" ? value.references_checked : null,
      broken,
      error: typeof value.error === "string" ? value.error : null,
    };
  } catch {
    return { filesChecked: null, referencesChecked: null, broken: [], error: "REPORT_UNREADABLE" };
  }
}

export type LoopPhase = "not_started" | "running" | "paused" | "completed" | "waiting_human" | "blocked" | "failed" | "cancelled";

export function loopPhase(status: string | null | undefined): LoopPhase {
  switch ((status || "").toUpperCase()) {
    case "": case "DRAFT": case "PENDING_APPROVAL": case "READY": return "not_started";
    case "RUNNING": case "VALIDATING": return "running";
    case "PAUSED": return "paused";
    case "COMPLETED": return "completed";
    case "WAITING_HUMAN": return "waiting_human";
    case "BLOCKED": return "blocked";
    case "CANCELLED": return "cancelled";
    default: return "failed";  // FAILED_*, TIMED_OUT, PARTIALLY_COMPLETED: not a result that can be submitted
  }
}

/** Whether stepping should stop: anything but a running loop waits for the person. */
export function isStopped(status: string | null | undefined): boolean {
  return loopPhase(status) !== "running" && loopPhase(status) !== "not_started";
}

export type CandidateRow = { candidate_id?: string; loop_id?: string; verification_status?: string; approval_status?: string | null; decision?: string | null };
export type CandidateState = "VERIFICATION_FAILED" | "WAITING_STEP_UP" | "ACCEPTED" | "REJECTED";

/** ACCEPTED only when the backend recorded an APPROVE decision (made with the Remote WebAuthn step-up). */
export function candidateState(row: CandidateRow | null | undefined): CandidateState | null {
  if (!row) return null;
  if (row.verification_status && row.verification_status !== "PASSED") return "VERIFICATION_FAILED";
  if (row.decision === "APPROVE") return "ACCEPTED";
  if (row.decision === "REJECT") return "REJECTED";
  return "WAITING_STEP_UP";
}

export type StepUp = { remote_console?: string; presence_device?: string; presence_verifier?: string; approvers?: number };
export type StepUpState = "remote_not_configured" | "no_presence_device" | "ready";

export function stepUpState(stepUp: StepUp | null | undefined): StepUpState {
  if (!stepUp || stepUp.remote_console !== "CONFIGURED") return "remote_not_configured";
  return stepUp.presence_device === "ENROLLED" ? "ready" : "no_presence_device";
}

export type FailureView = { key: MessageKey; code: string; requestId: string; status: number };

/** A refusal in plain words (a message key) plus the technical code to show on demand. */
export function failureView(error: unknown): FailureView {
  if (!(error instanceof HttpError)) {
    return { key: "error.generic", code: error instanceof Error ? error.message.slice(0, 200) : String(error).slice(0, 200), requestId: "", status: 0 };
  }
  const code = error.code.split(":")[0].trim();
  const specific = `error.${code}`;
  let key: MessageKey = "error.generic";
  if (hasMessage(specific)) key = specific;
  else if (error.outcome === "WRITE_OUTCOME_UNKNOWN") key = "error.WRITE_OUTCOME_UNKNOWN";
  else if (error.outcome === "NETWORK_ERROR" || error.outcome === "REQUEST_TIMEOUT") key = "error.NETWORK_ERROR";
  else if (/^SESSION_|^CSRF_|^ORIGIN_/.test(code)) key = "error.SESSION";
  return { key, code, requestId: error.requestId, status: error.status };
}

export type MarkdownBlock =
  | { kind: "heading"; level: number; parts: InlinePart[] }
  | { kind: "paragraph"; parts: InlinePart[] }
  | { kind: "item"; parts: InlinePart[] }
  | { kind: "code"; text: string };
export type InlinePart = { kind: "text"; text: string } | { kind: "link"; text: string; target: string; relative: boolean };

const LINK = /!?\[([^\]\n]*)\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"\n]*")?\s*\)/g;
const SCHEME = /^[A-Za-z][A-Za-z0-9+.-]*:/;

export function inlineParts(line: string): InlinePart[] {
  const parts: InlinePart[] = [];
  let last = 0;
  for (const match of line.matchAll(LINK)) {
    const index = match.index ?? 0;
    if (index > last) parts.push({ kind: "text", text: line.slice(last, index) });
    const target = match[2];
    parts.push({ kind: "link", text: match[1] || target, target, relative: !SCHEME.test(target) && !target.startsWith("#") && !target.startsWith("//") });
    last = index + match[0].length;
  }
  if (last < line.length) parts.push({ kind: "text", text: line.slice(last) });
  return parts;
}

/** A small, safe Markdown reading for the preview (headings, list items, paragraphs, fenced code, links as text). */
export function markdownBlocks(source: string): MarkdownBlock[] {
  const blocks: MarkdownBlock[] = [];
  let paragraph: string[] = [];
  let code: string[] | null = null;
  const flush = () => { if (paragraph.length) blocks.push({ kind: "paragraph", parts: inlineParts(paragraph.join(" ")) }); paragraph = []; };
  for (const line of source.replace(/\r\n?/g, "\n").split("\n")) {
    if (/^\s*(```|~~~)/.test(line)) {
      if (code) { blocks.push({ kind: "code", text: code.join("\n") }); code = null; } else { flush(); code = []; }
      continue;
    }
    if (code) { code.push(line); continue; }
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    const item = /^\s*(?:[-*+]|\d+[.)])\s+(.*)$/.exec(line);
    if (heading) { flush(); blocks.push({ kind: "heading", level: heading[1].length, parts: inlineParts(heading[2]) }); }
    else if (item) { flush(); blocks.push({ kind: "item", parts: inlineParts(item[1]) }); }
    else if (!line.trim()) flush();
    else paragraph.push(line.trim());
  }
  if (code) blocks.push({ kind: "code", text: code.join("\n") });
  flush();
  return blocks;
}

export function newKey(prefix: string): string {
  const bytes = new Uint8Array(12);
  globalThis.crypto.getRandomValues(bytes);
  return `${prefix}-${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

/**
 * Whether 產生計畫 can submit. The project <select> is `required` and controlled: until the project list has loaded,
 * it has no <option> for the chosen project, so its value is "" and the browser's form validation silently refuses
 * the submit. The button stays disabled until the chosen project is one of the loaded options.
 */
export function canMakePlan(input: { projects: readonly { id: string }[] | null | undefined; projectId: string;
  available: boolean; busy: boolean }): boolean {
  return !input.busy && input.available && Boolean(input.projectId)
    && (input.projects || []).some((project) => project.id === input.projectId);
}
