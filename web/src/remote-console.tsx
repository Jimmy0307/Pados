// SPDX-License-Identifier: AGPL-3.0-only
import { useState, type FormEvent, type ReactElement } from "react";
import {
  projectsOf,
  type AttentionProjection,
  type Banner,
  type RemoteNodes,
  type RemoteRun,
  type RemoteTaskProjection,
  type WorkMapProjection,
} from "./remote-api";

export type RemoteView = "map" | "tasks" | "attention" | "actions";
export const REMOTE_VIEWS: { id: RemoteView; label: string }[] = [
  { id: "map", label: "Map" },
  { id: "tasks", label: "Tasks" },
  { id: "attention", label: "Attention" },
  { id: "actions", label: "Actions" },
];

function RunRow({ run, onSelect }: { run: RemoteRun; onSelect?: () => void }): ReactElement {
  return <li className={`rc-run rc-${run.bucket.toLowerCase()}`}>
    <button className="rc-node-button" type="button" onClick={onSelect}>
      <span className="rc-run-head"><strong>{run.goal || run.run_id}</strong><span className="rc-tag">{run.bucket.replace("_", " ")}</span></span>
      <small>{run.project_id} · {run.progress_status || "Status unavailable"} · semantic commit {run.semantic_commit_status || "unavailable"}</small>
    </button>
  </li>;
}

function Section({ title, runs }: { title: string; runs: RemoteRun[] }): ReactElement {
  return <section className="rc-section" aria-label={title}>
    <h2>{title} <span className="rc-count">{runs.length}</span></h2>
    {runs.length === 0 ? <p className="rc-empty">Nothing here.</p> : <ul>{runs.map((r) =>
      <li key={r.run_id} className={`rc-run rc-${r.bucket.toLowerCase()}`}>
        <div className="rc-run-head"><strong>{r.goal || r.run_id}</strong><span className="rc-tag">{r.bucket.replace("_", " ")}</span></div>
        <small>{r.project_id} · {r.progress_status || "Status unavailable"} · semantic commit {r.semantic_commit_status || "unavailable"}</small>
      </li>)}</ul>}
  </section>;
}

export function AttentionView({ view }: { view: AttentionProjection }): ReactElement {
  return <div>
    <Section title="Needs attention" runs={view.needs_attention} />
    <Section title="Running" runs={view.running} />
    <Section title="Waiting approval" runs={view.waiting_approval} />
    <Section title="Failed" runs={view.failed} />
    <Section title="Completed" runs={view.completed} />
  </div>;
}

export function ProjectsView({ view }: { view: AttentionProjection }): ReactElement {
  const projects = projectsOf(view);
  return <section className="rc-section" aria-label="Projects">
    <h2>Projects</h2>
    {projects.length === 0 ? <p className="rc-empty">No projects visible to this actor.</p> :
      <ul>{projects.map((p) => <li key={p.project_id} className="rc-run"><strong>{p.project_id}</strong>
        <small>{p.counts.WAITING_APPROVAL} waiting · {p.counts.FAILED} failed · {p.counts.RUNNING} running · {p.counts.COMPLETED} completed</small></li>)}</ul>}
  </section>;
}

export function JobsView({ view, onControl }: { view: AttentionProjection; onControl?: (run: RemoteRun, action: "pause" | "cancel") => void }): ReactElement {
  const runs = [...view.running, ...view.waiting_approval, ...view.failed, ...view.completed];
  return <section className="rc-section" aria-label="Jobs">
    <h2>Jobs</h2>
    {runs.length === 0 ? <p className="rc-empty">No current executions.</p> : <ul>{runs.map((run) => <li key={run.run_id} className="rc-run">
      <RunRow run={run} />
      {run.bucket === "RUNNING" && onControl && <div className="rc-actions">
        <button type="button" onClick={() => onControl(run, "pause")}>Pause</button>
        <button type="button" onClick={() => onControl(run, "cancel")}>Cancel</button>
      </div>}
    </li>)}</ul>}
  </section>;
}

export function ActionsView({ view, onDecide, onEnroll }: { view: AttentionProjection; onDecide?: (run: RemoteRun, decision: "approve" | "reject") => void; onEnroll?: () => void }): ReactElement {
  return <section className="rc-section" aria-label="Actions">
    <h2>Pending decisions</h2>
    <p className="rc-note">Approval applies only to the exact candidate presented. Each decision asks your authenticator to verify you (passkey, biometric or PIN) and produces a single-use proof. A changed candidate or project scope causes PADOS to refuse the commit.</p>
    {view.waiting_approval.length === 0 ? <p className="rc-empty">No decisions pending.</p> :
      <ul>{view.waiting_approval.map((run) => <li key={run.run_id} className="rc-run">
        <RunRow run={run} />
        <div className="rc-actions">
          <button type="button" className="rc-approve" disabled={!run.approval_ref} onClick={() => onDecide?.(run, "approve")}>Approve</button>
          <button type="button" className="rc-reject" disabled={!run.approval_ref} onClick={() => onDecide?.(run, "reject")}>Reject</button>
        </div>
      </li>)}</ul>}
    {onEnroll && <button type="button" className="rc-enroll" onClick={onEnroll}>Register this device for approvals</button>}
  </section>;
}

export function NodesView({ nodes }: { nodes?: RemoteNodes }): ReactElement {
  return <section className="rc-section" aria-label="Compute availability">
    <h2>Compute availability</h2>
    {!nodes || nodes.nodes.length === 0 ? <p className="rc-empty">{nodes?.status || "No node data."}</p> :
      <ul>{nodes.nodes.map((n) => <li key={n.node_id} className="rc-run"><strong>{n.node_name || n.node_id}</strong>
        <small>{n.state}{n.stale ? " · stale" : ""}{n.authorized ? "" : " · not authorized"}</small></li>)}</ul>}
    <p className="rc-note">Availability only. Provider endpoints and access values are not exposed.</p>
  </section>;
}

export function ConnectivityBanner({ banner, lastSync }: { banner: Banner; lastSync?: string }): ReactElement {
  return <div className={`rc-banner rc-banner-${banner.kind}`} role="region" aria-label="Connectivity">
    <strong>{banner.text}</strong>
    {banner.kind !== "online" && lastSync && <small>Last successful view: {lastSync}. Project data is not cached on this device.</small>}
  </div>;
}

export function WorkMapView({
  projection,
  selectedId,
  onSelect,
  nodes,
}: {
  projection: WorkMapProjection;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  nodes?: RemoteNodes;
}): ReactElement {
  const [zoom, setZoom] = useState(100);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const selectedTask = projection.tasks.find((task) => task.task_id === selectedId);
  const selectedGoal = projection.tasks.flatMap((task) => task.goals.map((goal) => ({ task, goal })))
    .find(({ goal }) => goal.goal_id === selectedId);
  const selectedRun = projection.runs.find((run) => run.run_id === selectedId);

  return <div className="rc-workspace">
    <section className="rc-section rc-map-section" aria-label="PADOS Work Map">
      <div className="rc-section-heading"><div><p className="rc-eyebrow">PADOS WORK MAP</p><h2>Follow work from intent to outcome</h2></div>
        <div className="rc-map-tools" aria-label="Map controls">
          <span className="rc-projection">Rebuildable view · Home Base remains authoritative</span>
          <button type="button" aria-label="Zoom out" disabled={zoom <= 70} onClick={() => setZoom((value) => Math.max(70, value - 10))}>−</button>
          <span className="rc-zoom-level" aria-live="polite">{zoom}%</span>
          <button type="button" aria-label="Zoom in" disabled={zoom >= 130} onClick={() => setZoom((value) => Math.min(130, value + 10))}>+</button>
          <button type="button" onClick={() => setZoom(100)}>Reset</button>
        </div></div>
      <div className="rc-map-canvas" aria-label="Task topology" tabIndex={0}>
        {projection.tasks.length === 0 && <p className="rc-empty">No captured tasks for the projects available to this account.</p>}
        {projection.tasks.map((task) => <article className="rc-map-task" key={task.task_id} style={{ transform: `scale(${zoom / 100})`, transformOrigin: "top left" }}>
          <button className={`rc-map-origin ${selectedId === task.task_id ? "is-selected" : ""}`} type="button" onClick={() => onSelect(task.task_id)}>
            <span className="rc-node-kicker">ORIGINAL TASK</span><strong>{task.original_intent}</strong>
            <small>{task.project_id} · {task.priority} · {task.work_request_status.replaceAll("_", " ")}</small>
          </button>
          <div className="rc-map-task-controls">
            <span>{task.goals.length} goal{task.goals.length === 1 ? "" : "s"}</span>
            <button type="button" aria-expanded={!collapsed.has(task.task_id)} onClick={() => setCollapsed((current) => {
              const next = new Set(current);
              if (next.has(task.task_id)) next.delete(task.task_id); else next.add(task.task_id);
              return next;
            })}>{collapsed.has(task.task_id) ? "Expand goals" : "Collapse goals"}</button>
          </div>
          {!collapsed.has(task.task_id) && <div className="rc-goal-track" aria-label="Goals">
            {task.goals.map((goal) => <div className="rc-goal-branch" key={goal.goal_id}>
              <span className="rc-connector" aria-hidden="true" />
              <button className={`rc-map-goal ${selectedId === goal.goal_id ? "is-selected" : ""}`} type="button" onClick={() => onSelect(goal.goal_id)}>
                <span className="rc-node-kicker">GOAL · {goal.state}</span><strong>{goal.title}</strong>
                <small>{goal.required_capabilities.length ? goal.required_capabilities.join(" · ") : "Capability not allocated"}</small>
              </button>
              <div className="rc-worker-node"><span className="rc-worker-dot" />Not assigned</div>
            </div>)}
          </div>}
          {!collapsed.has(task.task_id) && <button className="rc-lineage-link" type="button" onClick={() => onSelect(task.task_id)}>Open task details →</button>}
        </article>)}
      </div>
      {projection.runs.length > 0 && <section className="rc-live-runs" aria-label="Product loop executions">
        <h3>Product loop executions</h3><p className="rc-note">Shown separately because current receipts do not bind these runs to the captured task envelopes.</p>
        <ul>{projection.runs.map((run) => <li key={run.run_id}><RunRow run={run} onSelect={() => onSelect(run.run_id)} /></li>)}</ul>
      </section>}
      <NodesView nodes={nodes} />
    </section>
    {(selectedTask || selectedGoal || selectedRun) && <aside className="rc-drawer" aria-label="Work details">
      <div className="rc-drawer-head"><div><p className="rc-eyebrow">WORK DETAILS</p><h2>{selectedGoal?.goal.title || selectedTask?.original_intent || selectedRun?.goal || selectedRun?.run_id}</h2></div>
        <button type="button" className="rc-close" aria-label="Close details" onClick={() => onSelect(null)}>×</button></div>
      <dl>
        <dt>Status</dt><dd>{selectedGoal?.goal.state || selectedTask?.work_request_status || selectedRun?.bucket || "Unavailable"}</dd>
        <dt>Assigned worker</dt><dd>{selectedTask?.assignments[0]?.worker_id || "Not assigned in the current projection"}</dd>
        <dt>Capability</dt><dd>{selectedGoal?.goal.required_capabilities.join(", ") || "Not allocated"}</dd>
        <dt>Why selected</dt><dd>Selection reason is not recorded for this item.</dd>
        <dt>Dependencies</dt><dd>{selectedGoal?.goal.dependencies.join(", ") || "None recorded"}</dd>
        <dt>Artifacts</dt><dd>{selectedTask?.artifacts.map((item) => item.artifact_id).join(", ") || selectedRun?.artifacts?.map((item) => item.ref).join(", ") || "None recorded"}</dd>
        <dt>Evidence and receipts</dt><dd>{selectedTask?.evidence.length || selectedTask?.receipts.length ? "Available in project records" : "None in this projection"}</dd>
        <dt>Remaining work</dt><dd>{selectedTask?.goals.filter((goal) => goal.state !== "COMPLETE").map((goal) => goal.title).join(", ") || "Not represented"}</dd>
        <dt>Scope</dt><dd>{selectedTask?.scope_version_ref || "See the authoritative run receipt"}</dd>
      </dl>
    </aside>}
  </div>;
}

export function TaskAssignmentView({
  tasks,
  projects,
  onCreate,
}: {
  tasks: RemoteTaskProjection[];
  projects: string[];
  onCreate: (project: string, intent: string, priority: "LOW" | "NORMAL" | "HIGH", deadline?: string) => Promise<boolean>;
}): ReactElement {
  const [project, setProject] = useState("");
  const [intent, setIntent] = useState("");
  const [priority, setPriority] = useState<"LOW" | "NORMAL" | "HIGH">("NORMAL");
  const [deadline, setDeadline] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    try {
      const deadlineIso = deadline ? new Date(deadline).toISOString() : undefined;
      const accepted = await onCreate(project.trim(), intent, priority, deadlineIso);
      if (accepted) {
        setIntent("");
        setDeadline("");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return <section className="rc-section rc-task-page" aria-label="Tasks">
    <header className="rc-task-heading"><div><p className="rc-eyebrow">TASKS</p><h2>Create a task</h2>
      <p className="rc-note">PADOS stores your original intent and creates a pending root goal. Goal decomposition and worker assignment appear when the authorized planner records them.</p></div></header>
    <form className="rc-task-form" aria-label="Create task" onSubmit={(event) => void submit(event)}>
      <label>Original intent<textarea aria-label="Original intent" maxLength={2000} value={intent} onChange={(event) => setIntent(event.target.value)} required /></label>
      <label>Project<select aria-label="Project" value={project} onChange={(event) => setProject(event.target.value)} required>
        <option value="" disabled>Select a project</option>{projects.map((projectId) => <option key={projectId} value={projectId}>{projectId}</option>)}
      </select></label>
      {projects.length === 0 && <p className="rc-note">No projects are available to this account.</p>}
      <div className="rc-form-row">
        <label>Priority<select aria-label="Priority" value={priority} onChange={(event) => setPriority(event.target.value as typeof priority)}>
          <option value="LOW">Low</option><option value="NORMAL">Normal</option><option value="HIGH">High</option>
        </select></label>
        <label>Deadline <span className="rc-optional">Optional</span><input aria-label="Deadline" type="datetime-local" value={deadline} onChange={(event) => setDeadline(event.target.value)} /></label>
      </div>
      <button type="submit" disabled={submitting || projects.length === 0}>{submitting ? "Sending intent…" : "Create task"}</button>
      <p className="rc-note">The browser sends intent only. Home Base authenticates, authorizes, and records the request.</p>
    </form>
    <div className="rc-task-list"><h2>Recent tasks <span className="rc-count">{tasks.length}</span></h2>
      {tasks.length === 0 ? <p className="rc-empty">No task envelopes are available.</p> : <ul>{tasks.map((task) => <li className="rc-run" key={task.task_id}>
        <div className="rc-run-head"><strong>{task.original_intent}</strong><span className="rc-tag">{task.work_request_status.replaceAll("_", " ")}</span></div>
        <small>{task.project_id} · {task.priority} · {task.goals.length} goal{task.goals.length === 1 ? "" : "s"}</small>
      </li>)}</ul>}
    </div>
  </section>;
}
