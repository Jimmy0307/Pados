// SPDX-License-Identifier: AGPL-3.0-only
// 工作需求 → 執行: a request in plain words, a readable Task Plan (a deterministic template when no AI is configured,
// labelled as such), 核准 / 修改 / 拒絕 on the existing WORK_ROUTE approval, the existing TaskLoop created, started and
// stepped through its own routes, Pause / Resume / Cancel, the Evidence (verified job artifacts), 提交候選結果, and the
// decision that only the Remote WebAuthn step-up can make. This page never shows a result as accepted unless the
// backend recorded that decision; the control plane's refusal (RESULT_ACCEPTANCE_REQUIRES_STEP_UP) is guidance here.
import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactElement } from "react";
import { formatDate, shortId } from "./api";
import { hasControlAuth, type ControlAuth } from "./control-auth";
import { isSessionLost } from "./identity";
import { useT } from "./i18n";
import { pageHref } from "./page-links";
import {
  controlLoop, createCapturedRequest, decide, getLoop, listProjects, loadEvidence, loopEvents, projectWork, proposePlan,
  startApprovedPlan, stepLoop, submitCandidate, type Approval, type Candidate, type Evidence, type Loop, type WorkRequest,
} from "./product-api";
import {
  CAPABILITY_OPTIONS, CHECK_CAPABILITY, CHECK_COMMAND, buildPlanTemplate, candidateState, isStopped, loopPhase, newKey,
  parseProbeReport, rootGoalId, stepUpState, type PlanTemplate,
} from "./product-flow";
import { useLoad } from "./product-load";
import { Blocked, Chip, Completed, Disabled, Failed, Loading, Section } from "./product-ui";

type Access = { auth: ControlAuth; onSessionLost?: (error: unknown) => void };
type Draft = { projectId: string; capability: string; title: string; text: string };
type Stage =
  | { kind: "compose"; notice?: "work.revised" }
  | { kind: "plan"; projectId: string; request: WorkRequest; plan: PlanTemplate; approval: Approval; folder: string }
  | { kind: "rejected" }
  | { kind: "run"; loopId: string };

const STEP_DELAY_MS = 400;
const MAX_STEPS_PER_VIEW = 12;

export function WorkPage({ auth, onSessionLost, initialProjectId, resumeLoopId, onOpenProject }: Access & {
  initialProjectId: string | null; resumeLoopId: string | null; onOpenProject: (projectId: string) => void;
}): ReactElement {
  const t = useT();
  const [stage, setStage] = useState<Stage>(resumeLoopId ? { kind: "run", loopId: resumeLoopId } : { kind: "compose" });
  const [draft, setDraft] = useState<Draft>({ projectId: initialProjectId || "", capability: CHECK_CAPABILITY,
    title: t("work.defaultTitle"), text: t("work.defaultText") });
  useEffect(() => { if (resumeLoopId) setStage({ kind: "run", loopId: resumeLoopId }); }, [resumeLoopId]);
  useEffect(() => { if (initialProjectId) setDraft((d) => ({ ...d, projectId: initialProjectId })); }, [initialProjectId]);
  if (!hasControlAuth(auth)) return <Disabled reason={t("auth.required")} />;
  const again = () => setStage({ kind: "compose" });
  return <>
    <div className="control-heading"><div><span className="control-eyebrow">PADOS</span><h1>{t("work.title")}</h1><p>{t("work.detail")}</p></div>
      {stage.kind !== "compose" && <button className="control-button" type="button" onClick={again}>{t("work.newRequest")}</button>}</div>
    {stage.kind === "compose" && <Compose auth={auth} onSessionLost={onSessionLost} draft={draft} onDraft={setDraft} notice={stage.notice}
      onPlanned={(next) => setStage(next)} />}
    {stage.kind === "plan" && <PlanReview auth={auth} onSessionLost={onSessionLost} stage={stage}
      onRevised={() => setStage({ kind: "compose", notice: "work.revised" })} onRejected={() => setStage({ kind: "rejected" })}
      onStarted={(loop) => setStage({ kind: "run", loopId: loop.id })} />}
    {stage.kind === "rejected" && <Completed message={t("work.rejected")} />}
    {stage.kind === "run" && <RunPanel key={stage.loopId} auth={auth} onSessionLost={onSessionLost} loopId={stage.loopId}
      onCheckAgain={(projectId) => { setDraft((d) => ({ ...d, projectId })); again(); }} onOpenProject={onOpenProject} />}
  </>;
}

function Compose({ auth, onSessionLost, draft, onDraft, notice, onPlanned }: Access & {
  draft: Draft; onDraft: (d: Draft) => void; notice?: "work.revised"; onPlanned: (stage: Stage) => void;
}): ReactElement {
  const t = useT();
  const projects = useLoad(() => listProjects(auth), [auth], onSessionLost);
  const [key, setKey] = useState(() => newKey("web-work"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const option = CAPABILITY_OPTIONS.find((item) => item.id === draft.capability) || CAPABILITY_OPTIONS[0];
  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { work_request: request } = await createCapturedRequest(auth, draft.projectId, draft.title, draft.text, option.id, key);
      const goal = rootGoalId(request.body);
      if (!goal) throw new Error("TASK_ENVELOPE_NOT_CAPTURED");
      const plan = buildPlanTemplate({ projectId: draft.projectId, requestId: request.id, rootGoalId: goal, title: draft.title, text: draft.text });
      const { approval } = await proposePlan(auth, draft.projectId, plan, newKey("web-plan"));
      const folder = projects.data?.projects.find((p) => p.id === draft.projectId)?.root_path?.split(/[\\/]/).pop() || "";
      setKey(newKey("web-work"));
      onPlanned({ kind: "plan", projectId: draft.projectId, request, plan, approval, folder });
    } catch (failure) {
      setError(failure);
      if (isSessionLost(failure)) onSessionLost?.(failure);
    } finally {
      setBusy(false);
    }
  }
  const list = projects.data?.projects || [];
  return <Section title={t("work.title")} testId="work-compose">
    {notice && <Blocked message={t(notice)} />}
    {projects.loading && <Loading />}
    {Boolean(projects.error) && <Failed error={projects.error} onRetry={projects.reload} />}
    {projects.data && list.length === 0 && <Disabled reason={t("work.noProjects")} />}
    <form className="control-form" aria-label={t("work.title")} onSubmit={(event) => void submit(event)}>
      <label>{t("work.project")}<select className="control-input" name="project" required value={draft.projectId} onChange={(e) => onDraft({ ...draft, projectId: e.target.value })}>
        <option value="">{t("work.chooseProject")}</option>{list.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label>
      <fieldset className="product-capabilities"><legend>{t("work.capability")}</legend>
        {CAPABILITY_OPTIONS.map((item) => <label key={item.id} className={item.available ? "" : "product-unavailable"}>
          <input type="radio" name="capability" value={item.id} checked={draft.capability === item.id} disabled={!item.available}
            onChange={() => onDraft({ ...draft, capability: item.id })} /> {t(item.labelKey)}
          {!item.available && <small> — {t("work.cap.unavailable")}</small>}</label>)}
        <small className="control-muted">{t("work.cap.check.help")}</small></fieldset>
      <label>{t("work.requestTitle")}<input className="control-input" name="title" required maxLength={300} value={draft.title} onChange={(e) => onDraft({ ...draft, title: e.target.value })} /></label>
      <label>{t("work.requestText")}<textarea className="control-input" name="text" required rows={4} maxLength={1600} value={draft.text} onChange={(e) => onDraft({ ...draft, text: e.target.value })} /></label>
      <button className="control-button product-primary" type="submit" disabled={busy || !draft.projectId || !option.available}>{busy ? t("state.loading") : t("work.makePlan")}</button>
    </form>
    {Boolean(error) && <Failed error={error} />}
  </Section>;
}

function PlanReview({ auth, onSessionLost, stage, onRevised, onRejected, onStarted }: Access & {
  stage: Extract<Stage, { kind: "plan" }>; onRevised: () => void; onRejected: () => void; onStarted: (loop: Loop) => void;
}): ReactElement {
  const t = useT();
  const [approval, setApproval] = useState(stage.approval);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  async function act(verb: "approve" | "reject" | "revise"): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      let current = approval;
      if (current.status === "PENDING") {
        current = (await decide(auth, current, verb, "")).approval;
        setApproval(current);
      }
      if (verb === "reject") return onRejected();
      if (verb === "revise") return onRevised();
      onStarted(await startApprovedPlan(auth, { projectId: stage.projectId, requestId: stage.request.id, approvalId: current.id, plan: stage.plan }));
    } catch (failure) {
      setError(failure);
      if (isSessionLost(failure)) onSessionLost?.(failure);
    } finally {
      setBusy(false);
    }
  }
  const pending = approval.status === "PENDING";
  return <Section title={t("work.plan")} testId="work-plan">
    <p className="product-badge" data-testid="plan-source">{t("work.plan.source")}</p>
    <p className="control-muted">{t("work.plan.sourceHelp")}</p>
    <dl className="control-details">
      <div><dt>{t("work.plan.actions")}</dt><dd><ol className="product-steps">{stage.plan.steps.map((step) =>
        <li key={String(step.sequence)}>{t("work.plan.step", { n: String(step.sequence), command: CHECK_COMMAND })}
          <small> · {String(step.required_capability)} · {stage.plan.allowedActions.join(", ")}</small></li>)}</ol></dd></div>
      <div><dt>{t("work.plan.risk")}</dt><dd><Chip value={stage.plan.risk} /></dd></div>
      <div><dt>{t("work.plan.scope")}</dt><dd>{t("work.plan.scopeValue", { folder: stage.folder || "—" })}</dd></div>
      <div><dt>WORK_ROUTE</dt><dd>{t("work.plan.approval", { status: approval.status })} · <small>{shortId(approval.id)}</small></dd></div>
    </dl>
    <div className="product-actions">
      <button className="control-button product-primary" type="button" disabled={busy || !["PENDING", "APPROVED"].includes(approval.status)} onClick={() => void act("approve")}>{t("work.approve")}</button>
      <button className="control-button" type="button" disabled={busy || !pending} onClick={() => void act("revise")}>{t("work.revise")}</button>
      <button className="control-button" type="button" disabled={busy || !pending} onClick={() => void act("reject")}>{t("work.reject")}</button>
    </div>
    {busy && <Loading />}
    {Boolean(error) && <Failed error={error} />}
  </Section>;
}

function RunPanel({ auth, onSessionLost, loopId, onCheckAgain, onOpenProject }: Access & {
  loopId: string; onCheckAgain: (projectId: string) => void; onOpenProject: (projectId: string) => void;
}): ReactElement {
  const t = useT();
  const [loop, setLoop] = useState<(Loop & { project_id?: string }) | null>(null);
  const [events, setEvents] = useState<Array<{ event_type: string; event_time?: string }>>([]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const steps = useRef(0);
  const refresh = useCallback(async () => {
    const [current, log] = await Promise.all([getLoop(auth, loopId), loopEvents(auth, loopId)]);
    setLoop(current.loop);
    setEvents(log.events);
    return current.loop;
  }, [auth, loopId]);
  useEffect(() => { refresh().catch((failure) => { setError(failure); if (isSessionLost(failure)) onSessionLost?.(failure); }); }, [refresh, onSessionLost]);
  // Drive the existing TaskLoop one step at a time while it runs; any other status waits for the person.
  useEffect(() => {
    if (!loop || loopPhase(loop.status) !== "running" || busy || error || steps.current >= MAX_STEPS_PER_VIEW) return;
    const timer = setTimeout(() => {
      steps.current += 1;
      setBusy(true);
      stepLoop(auth, loopId).then(() => refresh()).catch((failure) => setError(failure)).finally(() => setBusy(false));
    }, STEP_DELAY_MS);
    return () => clearTimeout(timer);
  }, [auth, loopId, loop, busy, error, refresh]);
  async function control(verb: "pause" | "resume" | "cancel"): Promise<void> {
    setError(null);
    try {
      await controlLoop(auth, loopId, verb);
      steps.current = 0;
      await refresh();
    } catch (failure) {
      setError(failure);
    }
  }
  if (!loop) return error ? <Failed error={error} /> : <Loading />;
  const phase = loopPhase(loop.status);
  const projectId = String(loop.project_id || "");
  return <>
    <Section title={t("work.run")} testId="work-run">
      <div className="control-row"><span>{t("work.run.status")}</span><span data-testid="loop-status"><Chip value={loop.status} /></span></div>
      <div className="control-row"><span>TaskLoop {shortId(loop.id)}</span><span>{t("work.run.progress", { current: loop.current_iteration ?? 0, max: loop.max_iterations ?? 0 })}</span></div>
      {phase === "running" && <Loading label={t("work.run.running")} />}
      {phase === "paused" && <Blocked message={t("work.run.paused")} />}
      {phase === "cancelled" && <Completed message={t("work.run.cancelled")} />}
      <div className="product-actions">
        <button className="control-button" type="button" disabled={phase !== "running"} onClick={() => void control("pause")}>{t("work.run.pause")}</button>
        <button className="control-button" type="button" disabled={phase !== "paused"} onClick={() => void control("resume")}>{t("work.run.resume")}</button>
        <button className="control-button" type="button" disabled={!["running", "paused", "waiting_human", "blocked"].includes(phase)} onClick={() => void control("cancel")}>{t("work.run.cancel")}</button>
      </div>
      <h3>{t("work.run.log")}</h3>
      <ul className="product-log" data-testid="loop-log">{events.map((event, index) => <li key={index}><code>{event.event_type}</code> <small>{formatDate(event.event_time)}</small></li>)}</ul>
      {Boolean(error) && <Failed error={error} onRetry={() => { setError(null); steps.current = 0; void refresh(); }} />}
    </Section>
    {isStopped(loop.status) && phase !== "cancelled" && <EvidencePanel auth={auth} loopId={loopId} />}
    {isStopped(loop.status) && projectId && <ResultPanel auth={auth} onSessionLost={onSessionLost} loop={loop} projectId={projectId}
      onCheckAgain={() => onCheckAgain(projectId)} onOpenProject={() => onOpenProject(projectId)} />}
  </>;
}

function EvidencePanel({ auth, loopId }: { auth: ControlAuth; loopId: string }): ReactElement {
  const t = useT();
  const evidence = useLoad<Evidence | null>(() => loadEvidence(auth, loopId), [auth, loopId]);
  const report = parseProbeReport(evidence.data?.stderr?.content);
  return <Section title={t("work.evidence")} testId="work-evidence">
    {evidence.loading && <Loading />}
    {Boolean(evidence.error) && <Failed error={evidence.error} onRetry={evidence.reload} />}
    {report && report.broken.length > 0 && <div className="product-broken" data-testid="broken-links"><strong>{t("work.evidence.broken")}</strong>
      <ul>{report.broken.map((item) => <li key={`${item.file}->${item.target}`}><code>{t("work.evidence.brokenItem", item)}</code></li>)}</ul></div>}
    {report && report.broken.length === 0 && !report.error && <Completed message={t("work.evidence.none", { count: report.referencesChecked ?? 0, files: report.filesChecked ?? 0 })} />}
    {evidence.data && <details className="product-receipts" open><summary>{t("work.evidence.receipts")}</summary>
      <p className="control-muted">job {shortId(evidence.data.jobId)} · {t("work.evidence.integrity", { integrity: evidence.data.stderr?.integrity || "—" })}</p>
      <ul>{evidence.data.artifacts.map((artifact) => <li key={`${artifact.artifact_type}-${artifact.sha256}`}><code>{artifact.artifact_type}</code> <small>SHA-256 {artifact.sha256}</small></li>)}</ul>
      {evidence.data.stdout && <pre className="product-pre">{evidence.data.stdout.content}</pre>}
    </details>}
  </Section>;
}

function ResultPanel({ auth, onSessionLost, loop, projectId, onCheckAgain, onOpenProject }: Access & {
  loop: Loop; projectId: string; onCheckAgain: () => void; onOpenProject: () => void;
}): ReactElement {
  const t = useT();
  const work = useLoad(() => projectWork(auth, projectId), [auth, projectId], onSessionLost);
  const [submitted, setSubmitted] = useState<Candidate | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const phase = loopPhase(loop.status);
  const row = work.data?.candidates.find((candidate) => candidate.loop_id === loop.id) || null;
  async function submit(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      setSubmitted((await submitCandidate(auth, loop.id)).candidate);
      work.reload();
    } catch (failure) {
      setError(failure);
      if (isSessionLost(failure)) onSessionLost?.(failure);
    } finally {
      setBusy(false);
    }
  }
  const state = candidateState(row || (submitted ? { verification_status: submitted.verification_status, decision: null } : null));
  const stepUp = stepUpState(work.data?.step_up);
  const remote = pageHref("remote", import.meta.env.BASE_URL, { view: "actions" });
  return <Section title={t("work.candidate")} testId="work-result">
    {phase === "waiting_human" && <Blocked message={t("work.blocked.human")} next={t("work.blocked.next")}>
      <button className="control-button" type="button" onClick={onOpenProject}>{t("work.fixDocs")}</button>
      <button className="control-button product-primary" type="button" onClick={onCheckAgain}>{t("work.checkAgain")}</button></Blocked>}
    {(phase === "blocked" || phase === "failed") && <Blocked message={`TaskLoop ${loop.status}`} next={t("work.blocked.next")}>
      <button className="control-button" type="button" onClick={onCheckAgain}>{t("work.checkAgain")}</button></Blocked>}
    {!state && (phase === "completed"
      ? <button className="control-button product-primary" type="button" disabled={busy} onClick={() => void submit()}>{t("work.submit")}</button>
      : <Disabled reason={t("work.submit.disabled")} />)}
    {busy && <Loading />}
    {Boolean(error) && <Failed error={error} />}
    {state === "VERIFICATION_FAILED" && <Failed error={new Error("RESULT_CANDIDATE_VERIFICATION_FAILED")} />}
    {state === "ACCEPTED" && <Completed message={t("work.candidate.accepted")} />}
    {state === "REJECTED" && <Blocked message={t("work.candidate.rejected")} />}
    {state === "WAITING_STEP_UP" && <div className="product-waiting" data-testid="candidate-waiting">
      <p><Chip value="WAITING_APPROVAL" /> <strong>{t("work.candidate.waiting")}</strong></p>
      <p>{t("work.candidate.verified", { status: row?.verification_status || submitted?.verification_status || "—" })} · approval <Chip value={row?.approval_status || "PENDING"} /></p>
      <p className="control-muted">{t("work.candidate.waitingHelp")}</p>
      {stepUp === "remote_not_configured" && <Blocked message={t("work.stepup.notConfigured")} next={t("work.stepup.notConfiguredNext")} />}
      {stepUp === "no_presence_device" && <Blocked message={t("work.stepup.noDevice")} next={t("work.stepup.noDeviceNext")} />}
      {stepUp === "ready" && <p>{t("work.stepup.ready")}</p>}
      <div className="product-actions"><a className="control-button" href={remote}>{t("work.candidate.open")}</a>
        <button className="control-button" type="button" onClick={work.reload}>{t("work.candidate.refresh")}</button></div>
    </div>}
    {work.data && <details className="product-audit"><summary>{t("work.audit")}</summary>
      <ul className="product-log">{work.data.events.slice(0, 30).map((event) => <li key={event.sequence_number}><code>{event.event_type}</code> <small>{formatDate(event.event_time)}</small></li>)}</ul>
      <p className="control-muted">WORK_ROUTE {shortId(String(loop.approval_id || ""))} · request {shortId(String(loop.request_id || ""))}{row?.approval_id ? ` · result approval ${shortId(row.approval_id)}` : ""}</p>
    </details>}
  </Section>;
}
