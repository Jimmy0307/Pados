// SPDX-License-Identifier: AGPL-3.0-only
// Local Control Center actions over the existing control API: create a work request, and review and decide an
// ordinary (work package) approval. Nothing here is a new authority. Every write goes to the same /control routes the
// CLI scripts use, with the same control token (X-PADOS-Control-Token), and the server decides what is allowed:
// an approval that backs a ResultCandidate is refused there (RESULT_ACCEPTANCE_REQUIRES_STEP_UP) and is decided only
// in the Remote console with a fresh WebAuthn step-up. A decision is sent only after the person confirms the exact
// approval, project, summary and version. The token lives in this page's memory only; it is never stored.
import { useEffect, useState, type FormEvent, type ReactElement } from "react";
import { describeFailure, requestJson } from "./http";
import { inboxAction, pageHref } from "./page-links";
import type { DecisionInboxItem, Project } from "./api";

export const CONTROL_TOKEN_HEADER = "X-PADOS-Control-Token";
export const LOCAL_ONLY = "CONTROL_TOKEN_LOCAL_ONLY";
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);

/** The control token is only ever typed into, and sent from, a page this computer serves to itself. */
export function isLocalPage(hostname: string | undefined = globalThis.location?.hostname): boolean {
  return LOOPBACK_HOSTS.has(String(hostname || "").toLowerCase());
}
export type ApprovalVerb = "approve" | "reject" | "revise";

type ApprovalSubject = {
  request_id?: string;
  actor?: { surfaces?: string[]; worker_types?: string[] };
  operations?: Array<{ capability_id?: string; command_id?: string | null; max_risk_level?: string; step_sequence?: number | null }>;
};

export type ControlApproval = {
  id: string;
  project_id: string;
  status: string;
  version: number | string;
  proposal_summary?: string | null;
  proposal_hash?: string | null;
  subject_kind?: string | null;
  subject_json?: ApprovalSubject | null;
  expires_at?: string | null;
  created_at?: string | null;
};

function idempotencyKey(prefix: string): string {
  const bytes = new Uint8Array(12);
  globalThis.crypto.getRandomValues(bytes);
  return `${prefix}-${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

/** A /control request with the operator's control token; errors keep the server's pados.error.v1 code. */
export function controlRequest<T>(path: string, controlValue: string, init: RequestInit = {}, fetcher?: typeof fetch): Promise<T> {
  // A build of this page served from any other host (a static site) must never carry the token to that host.
  if (!isLocalPage()) return Promise.reject(new Error(`${LOCAL_ONLY}: open the console on this computer (127.0.0.1)`));
  const headers = { Accept: "application/json", "Content-Type": "application/json", [CONTROL_TOKEN_HEADER]: controlValue };
  return requestJson<T>(path, { ...init, headers: { ...headers, ...(init.headers || {}) } }, fetcher ? { fetcher } : {});
}

export function createWorkRequest(controlValue: string, projectId: string, title: string, body: string,
                                  fetcher?: typeof fetch, key: string = idempotencyKey("web-work"),
): Promise<{ work_request: { id: string; status?: string } }> {
  return controlRequest("/control/work-requests", controlValue, {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, title, body, idempotency_key: key }),
  }, fetcher);
}

export function getApproval(controlValue: string, approvalId: string, fetcher?: typeof fetch): Promise<{ approval: ControlApproval }> {
  return controlRequest(`/control/approvals/${encodeURIComponent(approvalId)}`, controlValue, {}, fetcher);
}

export function decideApproval(controlValue: string, approval: ControlApproval, verb: ApprovalVerb, reason: string,
                               fetcher?: typeof fetch): Promise<{ approval: ControlApproval }> {
  return controlRequest(`/control/approvals/${encodeURIComponent(approval.id)}/${verb}`, controlValue, {
    method: "POST",
    body: JSON.stringify({ version: Number(approval.version), reason, idempotency_key: idempotencyKey(`web-${verb}`) }),
  }, fetcher);
}

/** What the approval would authorize, one line per operation (capability, command, maximum risk). */
export function describeSubject(approval: ControlApproval): string[] {
  const subject = approval.subject_json;
  if (!subject) return [approval.subject_kind ? `${approval.subject_kind} (no readable subject)` : "no subject: it cannot route work"];
  const operations = (subject.operations ?? []).map((operation) =>
    `${operation.capability_id ?? "?"}${operation.command_id ? ` ${operation.command_id}` : ""} · risk up to ${operation.max_risk_level ?? "?"}`);
  return [`request ${subject.request_id ?? "?"} · workers ${(subject.actor?.worker_types ?? []).join(", ") || "?"}`, ...operations];
}

/** The text a person confirms before a decision is sent: what exactly is being decided. */
export function decisionPrompt(approval: ControlApproval, verb: ApprovalVerb): string {
  return [
    `${verb.toUpperCase()} approval ${approval.id}`,
    `project ${approval.project_id} · version ${approval.version} · ${approval.subject_kind || "no subject"}`,
    ...describeSubject(approval),
    `summary: ${(approval.proposal_summary || "").slice(0, 300)}`,
    "",
    "This records your decision with the control token. Continue?",
  ].join("\n");
}

export function ControlTokenPanel({ controlValue, onChange, local = isLocalPage() }: { controlValue: string; onChange: (value: string) => void; local?: boolean }): ReactElement {
  const [draft, setDraft] = useState("");
  if (!local) {
    return <p className="control-error" role="alert">Changes are made only from the console on this computer
      (http://127.0.0.1). This page is served from another host, so it does not take the control token.</p>;
  }
  if (controlValue) {
    return <div className="control-row control-token"><span>Control token entered for this page only.</span>
      <button className="control-button" type="button" onClick={() => onChange("")}>Forget token</button></div>;
  }
  return <form className="control-token" aria-label="Control token" onSubmit={(event) => { event.preventDefault(); onChange(draft.trim()); setDraft(""); }}>
    <label>Control token <small>From the file pados init wrote (control_token_file). Kept in this page's memory only; reload forgets it.</small>
      <input className="control-input" name="control-token" type="password" autoComplete="off" value={draft} onChange={(event) => setDraft(event.target.value)} /></label>
    <button className="control-button" type="submit" disabled={!draft.trim()}>Use token</button>
  </form>;
}

export function WorkRequestPage({ controlValue, projects }: { controlValue: string; projects: Project[] }): ReactElement {
  const [projectId, setProjectId] = useState("");
  // One key per request being written: a retry after an unknown outcome is the same request, not a second one.
  const [requestKey, setRequestKey] = useState(() => idempotencyKey("web-work"));
  const [notice, setNotice] = useState("");
  const [created, setCreated] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    setBusy(true);
    setNotice("");
    try {
      const result = await createWorkRequest(controlValue, projectId, String(form.get("title") || ""), String(form.get("body") || ""),
        undefined, requestKey);
      setCreated(result.work_request.id);
      setRequestKey(idempotencyKey("web-work"));
      formElement.reset();
    } catch (error) {
      setNotice(`Not created: ${describeFailure(error)}`);
    } finally {
      setBusy(false);
    }
  }
  return <section className="control-card"><h2>New work request</h2>
    <p className="control-muted">Records what you want done. It does not run anything: routing it needs an approved work route (see the local usage guide).</p>
    {!projects.length && <p className="control-muted">No projects yet. Create one with pados project create.</p>}
    <form className="control-form" aria-label="New work request" onSubmit={(event) => void submit(event)}>
      <label>Project<select className="control-input" required value={projectId} onChange={(event) => setProjectId(event.target.value)}>
        <option value="">Choose a project</option>
        {projects.map((project) => <option key={project.id} value={project.id}>{project.name || project.slug || project.id}</option>)}
      </select></label>
      <label>Title<input className="control-input" name="title" required maxLength={300} /></label>
      <label>What should be done<textarea className="control-input" name="body" required rows={5} /></label>
      <button className="control-button" type="submit" disabled={!controlValue || busy}>{controlValue ? "Create work request" : "Enter the control token first"}</button>
    </form>
    {created && <p role="status">Created work request {created}.</p>}
    {notice && <p className="control-error" role="alert">{notice}</p>}
  </section>;
}

export function ApprovalsPage({ controlValue, items, focus }: { controlValue: string; items: DecisionInboxItem[]; focus: string | null }): ReactElement {
  const approvals = items.filter((item) => item.type === "APPROVAL");
  const [selected, setSelected] = useState<string | null>(focus);
  useEffect(() => setSelected(focus), [focus]);
  return <section className="control-card"><h2>Pending approvals</h2>
    {approvals.length === 0 && <p className="control-muted">No pending approvals.</p>}
    <div className="control-table-wrap"><table className="control-table"><thead><tr><th>Project</th><th>Item</th><th>Required action</th><th /></tr></thead>
      <tbody>{approvals.map((item) => {
        const action = inboxAction(item.open, item.target, import.meta.env.BASE_URL);
        return <tr key={item.id}><td>{item.project}</td><td>{item.item}</td><td>{item.required_human_action}</td><td>
          {action.kind === "link" && <a href={action.href}>{action.label}</a>}
          {action.kind === "in-console" && <button className="control-button" type="button" onClick={() => setSelected(action.approvalId)}>Review</button>}
        </td></tr>;
      })}</tbody></table></div>
    {selected && <ApprovalReview key={selected} controlValue={controlValue} approvalId={selected} />}
  </section>;
}

function ApprovalReview({ controlValue, approvalId }: { controlValue: string; approvalId: string }): ReactElement {
  const [approval, setApproval] = useState<ControlApproval | null>(null);
  const [notice, setNotice] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!controlValue) return;
    let current = true;  // a late answer for an earlier selection or token is dropped
    getApproval(controlValue, approvalId).then((result) => { if (current) setApproval(result.approval); })
      .catch((error) => { if (current) setNotice(describeFailure(error)); });
    return () => { current = false; };
  }, [controlValue, approvalId]);
  if (!controlValue) return <p className="control-muted">Enter the control token above to review approval {approvalId}.</p>;
  if (!approval) return <p className="control-muted" role="status">{notice || `Loading approval ${approvalId}…`}</p>;
  async function decide(verb: ApprovalVerb): Promise<void> {
    if (!approval || !window.confirm(decisionPrompt(approval, verb))) return;
    setBusy(true);
    try {
      const result = await decideApproval(controlValue, approval, verb, reason);
      setApproval(result.approval);
      setNotice(`Recorded: ${result.approval.status}.`);
    } catch (error) {
      setNotice(`Not recorded: ${describeFailure(error)}`);
    } finally {
      setBusy(false);
    }
  }
  const pending = approval.status === "PENDING";
  return <div className="control-review" aria-label={`Approval ${approval.id}`}>
    <h3>Approval {approval.id}</h3>
    <dl className="control-details">
      <div><dt>Status</dt><dd>{approval.status}</dd></div>
      <div><dt>Project</dt><dd>{approval.project_id}</dd></div>
      <div><dt>Version</dt><dd>{String(approval.version)}</dd></div>
      <div><dt>Authorizes</dt><dd>{describeSubject(approval).map((line) => <div key={line}>{line}</div>)}</dd></div>
      <div><dt>Summary</dt><dd>{approval.proposal_summary || "—"}</dd></div>
      <div><dt>Expires</dt><dd>{approval.expires_at || "—"}</dd></div>
    </dl>
    {pending && <><label>Reason (optional)<input className="control-input" maxLength={1000} value={reason} onChange={(event) => setReason(event.target.value)} /></label>
      <div className="control-row">
        <button className="control-button" type="button" disabled={busy} onClick={() => void decide("approve")}>Approve</button>
        <button className="control-button" type="button" disabled={busy} onClick={() => void decide("reject")}>Reject</button>
        <button className="control-button" type="button" disabled={busy} onClick={() => void decide("revise")}>Request revision</button>
      </div></>}
    {notice && <p role="status">{notice}</p>}
    <p className="control-muted">A verified result waiting for acceptance is not decided here: open it in the <a href={pageHref("remote", import.meta.env.BASE_URL, { view: "actions" })}>Remote console</a>.</p>
  </div>;
}
