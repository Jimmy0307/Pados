// SPDX-License-Identifier: AGPL-3.0-only
// 專案 Projects: the list, 建立專案 (a name and a description only: the server derives the folder inside the approved
// workspace), and a project's page: goals and tasks, execution history, ResultCandidates and their decisions, Markdown
// documents (新增 Markdown with a preview, edit with optimistic concurrency), knowledge and the audit events. Every
// read and write is a /control route, sent with this browser's paired session or the control token.
import { useState, type FormEvent, type ReactElement } from "react";
import { formatDate, shortId } from "./api";
import { hasControlAuth, type ControlAuth } from "./control-auth";
import { isSessionLost } from "./identity";
import { useT } from "./i18n";
import {
  contextSummary, createProject, listDocuments, listProjects, projectMessages, projectWork, readDocument, writeDocument,
  type DocumentReceipt, type ProjectWork,
} from "./product-api";
import { candidateState, newKey } from "./product-flow";
import { useLoad } from "./product-load";
import { Blocked, Chip, Completed, Disabled, Empty, Failed, Loading, MarkdownPreview, Section } from "./product-ui";

type Access = { auth: ControlAuth; onSessionLost?: (error: unknown) => void };
type Navigation = { selected: string | null; onSelect: (projectId: string | null) => void; onOpenWork: (projectId: string, loopId?: string) => void };

export function ProjectsPage({ auth, onSessionLost, selected, onSelect, onOpenWork }: Access & Navigation): ReactElement {
  const t = useT();
  if (!hasControlAuth(auth)) return <Disabled reason={t("auth.required")} />;
  if (selected) return <ProjectDetail auth={auth} onSessionLost={onSessionLost} projectId={selected} onBack={() => onSelect(null)} onOpenWork={onOpenWork} />;
  return <ProjectList auth={auth} onSessionLost={onSessionLost} onSelect={onSelect} />;
}

function ProjectList({ auth, onSessionLost, onSelect }: Access & { onSelect: (projectId: string) => void }): ReactElement {
  const t = useT();
  const projects = useLoad(() => listProjects(auth), [auth], onSessionLost);
  return <>
    <div className="control-heading"><div><span className="control-eyebrow">PADOS</span><h1>{t("projects.title")}</h1><p>{t("projects.detail")}</p></div></div>
    <Section title={t("projects.title")} testId="project-list">
      {projects.loading && <Loading />}
      {Boolean(projects.error) && <Failed error={projects.error} onRetry={projects.reload} />}
      {projects.data && projects.data.projects.length === 0 && <Empty messageKey="projects.empty" />}
      {projects.data && projects.data.projects.length > 0 && <div className="control-table-wrap"><table className="control-table product-table">
        <thead><tr><th>{t("projects.name")}</th><th>{t("projects.status")}</th><th>{t("projects.updated")}</th><th /></tr></thead>
        <tbody>{projects.data.projects.map((project) => <tr key={project.id}>
          <td><strong>{project.name}</strong></td><td><Chip value={project.status} /></td><td>{formatDate(project.updated_at)}</td>
          <td><button className="control-button" type="button" onClick={() => onSelect(project.id)} aria-label={`${t("projects.open")} ${project.name}`}>{t("projects.open")}</button></td>
        </tr>)}</tbody></table></div>}
    </Section>
    <CreateProject auth={auth} onSessionLost={onSessionLost} onCreated={(id) => { projects.reload(); onSelect(id); }} />
  </>;
}

function CreateProject({ auth, onSessionLost, onCreated }: Access & { onCreated: (projectId: string) => void }): ReactElement {
  const t = useT();
  const [key, setKey] = useState(() => newKey("web-project"));  // one key per project being created: a retry replays
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    setError(null);
    try {
      const created = await createProject(auth, String(form.get("name") || ""), String(form.get("description") || ""), key);
      setKey(newKey("web-project"));
      onCreated(created.project.id);
    } catch (failure) {
      setError(failure);
      if (isSessionLost(failure)) onSessionLost?.(failure);
    } finally {
      setBusy(false);
    }
  }
  return <Section title={t("projects.create.title")} testId="create-project">
    <p className="control-muted">{t("projects.create.help")}</p>
    <form className="control-form" aria-label={t("projects.create.title")} onSubmit={(event) => void submit(event)}>
      <label>{t("projects.name")}<input className="control-input" name="name" required maxLength={120} /></label>
      <label>{t("projects.description")}<textarea className="control-input" name="description" rows={3} maxLength={2000} /></label>
      <button className="control-button product-primary" type="submit" disabled={busy}>{busy ? t("state.loading") : t("projects.create")}</button>
    </form>
    {Boolean(error) && <Failed error={error} />}
  </Section>;
}

function ProjectDetail({ auth, onSessionLost, projectId, onBack, onOpenWork }: Access & { projectId: string; onBack: () => void; onOpenWork: (projectId: string, loopId?: string) => void }): ReactElement {
  const t = useT();
  const work = useLoad(() => projectWork(auth, projectId), [auth, projectId], onSessionLost);
  const project = work.data?.project;
  return <>
    <button className="control-back product-back" type="button" onClick={onBack}>{t("projects.back")}</button>
    <div className="control-heading"><div><span className="control-eyebrow">{t("projects.title")}</span>
      <h1 data-testid="project-name">{project?.name || shortId(projectId)}</h1>
      <p>{project?.description || " "}</p>
      {project?.folder && <p className="control-muted">{t("projects.folder")}: <code>{project.folder}</code></p>}</div>
      <button className="control-button product-primary" type="button" onClick={() => onOpenWork(projectId)}>{t("projects.ask")}</button></div>
    {work.loading && !work.data && <Loading />}
    {Boolean(work.error) && <Failed error={work.error} onRetry={work.reload} />}
    {work.data && <WorkSummary work={work.data} onOpenRun={(loopId) => onOpenWork(projectId, loopId)} />}
    <Documents auth={auth} onSessionLost={onSessionLost} projectId={projectId} onSaved={work.reload} />
    {work.data && <Knowledge auth={auth} projectId={projectId} work={work.data} />}
  </>;
}

function WorkSummary({ work, onOpenRun }: { work: ProjectWork; onOpenRun: (loopId: string) => void }): ReactElement {
  const t = useT();
  const stepsByGoal = new Map(work.plans.map((plan) => [plan.task_goal_id, plan]));
  return <div className="product-grid">
    <Section title={t("project.goals")} testId="project-goals">
      {work.goals.length === 0 ? <Empty messageKey="project.goals.empty" /> : work.goals.map((goal) => {
        const plan = stepsByGoal.get(goal.id);
        return <div className="control-row product-row" key={goal.id}><div><strong>{goal.objective}</strong>
          {plan?.steps.map((step) => <small key={step.sequence}>{step.sequence}. {step.title} · {step.required_capability} · {step.command_id || "—"} · risk {step.risk_level} · <Chip value={step.status} /></small>)}</div>
          <Chip value={goal.status} /></div>;
      })}
    </Section>
    <Section title={t("project.history")} testId="project-history">
      {work.loops.length === 0 ? <Empty messageKey="project.history.empty" /> : work.loops.map((loop) => <div className="control-row product-row" key={loop.id}>
        <div><strong>TaskLoop {shortId(loop.id)}</strong><small>{formatDate(loop.created_at)} · {t("work.run.progress", { current: loop.current_iteration ?? 0, max: loop.max_iterations ?? 0 })}</small></div>
        <Chip value={loop.status} /><button className="control-button" type="button" onClick={() => onOpenRun(loop.id)}>{t("project.openRun")}</button></div>)}
    </Section>
    <Section title={t("project.candidates")} testId="project-candidates">
      {work.candidates.length === 0 ? <Empty messageKey="project.candidates.empty" /> : work.candidates.map((candidate) => {
        const state = candidateState(candidate);
        return <div className="control-row product-row" key={candidate.candidate_id}>
          <div><strong>{shortId(candidate.candidate_id || "")}</strong><small>verification {candidate.verification_status} · approval {candidate.approval_status || "—"} · decision {candidate.decision || "—"}</small></div>
          <Chip value={state === "WAITING_STEP_UP" ? "WAITING_STEP_UP" : state} /></div>;
      })}
    </Section>
  </div>;
}

function Knowledge({ auth, projectId, work }: { auth: ControlAuth; projectId: string; work: ProjectWork }): ReactElement {
  const t = useT();
  const memory = useLoad(() => contextSummary(auth, projectId), [auth, projectId]);
  const messages = useLoad(() => projectMessages(auth, projectId), [auth, projectId]);
  const counts = memory.data?.context.memory_counts || [];
  return <div className="product-grid">
    <Section title={t("project.knowledge")} testId="project-knowledge">
      {counts.length === 0 ? <Empty messageKey="project.knowledge.empty" /> : counts.map((row) => <div className="control-row" key={`${row.verification_status}-${row.freshness_status}`}>
        <span>{row.verification_status} · {row.freshness_status}</span><strong>{row.count}</strong></div>)}
      <p className="control-muted">{t("project.messages", { count: messages.data?.messages.length ?? 0 })}</p>
    </Section>
    <Section title={t("project.audit")} testId="project-audit">
      {work.events.length === 0 ? <Empty messageKey="project.audit.empty" /> : <ul className="product-log">{work.events.slice(0, 20).map((event) =>
        <li key={event.sequence_number}><code>{event.event_type}</code> <small>{formatDate(event.event_time)}</small></li>)}</ul>}
    </Section>
  </div>;
}

const STARTER = "# 使用指南\n\n請先閱讀[專案說明](README.md)。\n";

function Documents({ auth, onSessionLost, projectId, onSaved }: Access & { projectId: string; onSaved: () => void }): ReactElement {
  const t = useT();
  const docs = useLoad(() => listDocuments(auth, projectId), [auth, projectId], onSessionLost);
  const [editing, setEditing] = useState<{ path: string; content: string; sha: string | null } | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  async function edit(path: string): Promise<void> {
    setLoadError(null);
    try {
      const { document } = await readDocument(auth, projectId, path);
      setEditing({ path: document.path, content: document.content, sha: document.sha256 });
    } catch (error) {
      setLoadError(error);
    }
  }
  const writable = docs.data?.writable ?? false;
  return <Section title={t("docs.title")} testId="project-documents">
    {docs.loading && !docs.data && <Loading />}
    {Boolean(docs.error) && <Failed error={docs.error} onRetry={docs.reload} />}
    {docs.data && docs.data.documents.length === 0 && <Empty messageKey="docs.empty" />}
    {docs.data && docs.data.documents.length > 0 && <ul className="product-doc-list">{docs.data.documents.map((doc) => <li key={doc.path}>
      <code>{doc.path}</code> <small>{doc.size} B · SHA-256 {(doc.sha256 || "—").slice(0, 12)}</small>
      {writable && <button className="control-button" type="button" onClick={() => void edit(doc.path)} aria-label={`${t("docs.edit")} ${doc.path}`}>{t("docs.edit")}</button>}
    </li>)}</ul>}
    {Boolean(loadError) && <Failed error={loadError} />}
    {docs.data && !writable && <Blocked message={t("docs.readOnly")} />}
    {docs.data && writable && <DocumentEditor key={editing ? `${editing.path}:${editing.sha}` : "new"} auth={auth} onSessionLost={onSessionLost} projectId={projectId}
      initial={editing} onSaved={() => { setEditing(null); docs.reload(); onSaved(); }} />}
  </Section>;
}

function DocumentEditor({ auth, onSessionLost, projectId, initial, onSaved }: Access & { projectId: string; initial: { path: string; content: string; sha: string | null } | null; onSaved: () => void }): ReactElement {
  const t = useT();
  const [path, setPath] = useState(initial?.path || "guide.md");
  const [content, setContent] = useState(initial?.content || STARTER);
  const [key] = useState(() => newKey("web-doc"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [saved, setSaved] = useState<DocumentReceipt | null>(null);
  async function save(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { document } = await writeDocument(auth, projectId, { path, content, replace: Boolean(initial), expectedSha256: initial?.sha }, key);
      setSaved(document);
      onSaved();
    } catch (failure) {
      setError(failure);
      if (isSessionLost(failure)) onSessionLost?.(failure);
    } finally {
      setBusy(false);
    }
  }
  return <form className="product-editor" aria-label={initial ? `${t("docs.edit")} ${initial.path}` : t("docs.add")} onSubmit={(event) => void save(event)}>
    <h3>{initial ? `${t("docs.edit")} ${initial.path}` : t("docs.add")}</h3>
    <label>{t("docs.path")}<input className="control-input" name="document-path" value={path} readOnly={Boolean(initial)} maxLength={200} onChange={(event) => setPath(event.target.value)} /></label>
    <div className="product-editor-panes">
      <label>{t("docs.content")}<textarea className="control-input product-editor-text" name="document-content" rows={12} value={content} onChange={(event) => setContent(event.target.value)} /></label>
      <div><span className="control-muted">{t("docs.preview")}</span><MarkdownPreview source={content} /><small className="control-muted">{t("docs.brokenHint")}</small></div>
    </div>
    <button className="control-button product-primary" type="submit" disabled={busy || !path.trim()}>{busy ? t("state.loading") : t("docs.save")}</button>
    {saved && <Completed message={t("docs.saved", { path: saved.path, sha: saved.sha256.slice(0, 12) })} />}
    {Boolean(error) && <Failed error={error} />}
  </form>;
}
