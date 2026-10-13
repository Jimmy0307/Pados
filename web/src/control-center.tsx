// SPDX-License-Identifier: AGPL-3.0-only
import { useResource } from "./useResource";
import { EnterprisePanel } from "./enterprise-panel";
import { ComputeNodesPanel } from "./compute-nodes-panel";
import { useState, type ReactElement } from "react";
import {
  formatDate,
  getJson,
  queryString,
  shortId,
  type ApiStatus,
  type ControlActivity,
  type ControlDatabaseOverview,
  type ControlHealth,
  type ControlMemory,
  type ControlTableRows,
  type DecisionInbox,
  type MissionControl,
  type Project,
} from "./api";

type Page = "mission" | "inbox" | "dashboard" | "projects" | "memory" | "database" | "runs" | "system";

const pages: Array<{ id: Page; label: string }> = [
  { id: "mission", label: "Mission Control" },
  { id: "inbox", label: "Decision / Action Inbox" },
  { id: "dashboard", label: "Dashboard" },
  { id: "projects", label: "Projects" },
  { id: "memory", label: "Memory" },
  { id: "database", label: "Database" },
  { id: "runs", label: "Runs & Events" },
  { id: "system", label: "System" },
];

function useJson<T>(path: string): { data: T | null; loading: boolean; error: string | null } {
  const resource = useResource<T>(path, () => getJson<T>(path));
  return { ...resource, error: resource.error instanceof Error ? resource.error.message : resource.error ? "Request failed" : null };
}

function StatusTag({ status }: { status?: string | null }): ReactElement {
  const normalized = (status || "UNKNOWN").toUpperCase() as ApiStatus;
  return <span className={`control-status control-status-${normalized.toLowerCase()}`}>{normalized}</span>;
}

function StatePanel({ loading, error, empty = "No records" }: { loading: boolean; error: string | null; empty?: string }): ReactElement {
  if (loading) return <div className="control-state">Loading local data…</div>;
  if (error) return <div className="control-state control-error"><strong>Surface unavailable</strong><span>{error}</span></div>;
  return <div className="control-state">{empty}</div>;
}

function DashboardPage(): ReactElement {
  const health = useJson<ControlHealth>("/api/health");
  const overview = useJson<ControlDatabaseOverview>("/api/database/overview");
  const events = useJson<{ events: ControlActivity[] }>("/api/events?page_size=5");
  if (health.loading || overview.loading) return <StatePanel loading={true} error={null} />;
  if (health.error || overview.error || !health.data || !overview.data) return <StatePanel loading={false} error={health.error || overview.error || "Dashboard unavailable"} />;
  return <>
    <div className="control-heading"><div><span className="control-eyebrow">Operational view</span><h1>PADOS at a glance</h1><p>Read-only runtime, memory, and database visibility.</p></div><button className="control-button" onClick={() => window.location.reload()}>Refresh</button></div>
    <div className="control-metrics"><Metric label="Runtime" value={<StatusTag status={health.data.status} />} /><Metric label="Database" value={health.data.database.name} /><Metric label="Projects" value={health.data.metrics.projects} /><Metric label="Memory" value={health.data.metrics.memories} /></div>
    <div className="control-grid"><section className="control-card"><h2>Readiness</h2>{health.data.checks.map((check) => <div className="control-row" key={check.name}><span>{check.name}</span><StatusTag status={check.status} /></div>)}</section><section className="control-card"><h2>Data map</h2>{overview.data.data_map.map((item) => <div className="control-row" key={item.entity}><strong>{item.entity}</strong><span>{item.tables.join(" · ")}</span></div>)}</section><section className="control-card"><h2>Latest activity</h2>{events.data?.events.map((event) => <div className="control-row" key={`${event.source}-${event.id}`}><StatusTag status={event.status} /><span>{event.name} · {formatDate(event.timestamp)}</span></div>) || <p className="control-muted">No events returned.</p>}</section></div>
  </>;
}

function MissionControlPage(): ReactElement {
  const result = useJson<MissionControl>("/api/mission-control");
  if (result.loading || result.error || !result.data) return <StatePanel loading={result.loading} error={result.error} />;
  const health = result.data.system_health;
  return <>
    <Header title="Mission Control" detail={`${result.data.projects.length} registered projects · live PADOS projection`} />
    <section className="control-card">
      <h2>System health · {health.state.toUpperCase()}</h2>
      <div className="control-metrics">
        {(["alive", "configured", "usable", "ready", "degraded", "blocked"] as const).map((key) => <Metric key={key} label={key} value={<StatusTag status={health[key] ? "OK" : "WARNING"} />} />)}
      </div>
      {health.checks.map((check) => <div className="control-row" key={check.name}><span>{check.name}</span><StatusTag status={check.status} /><small>{check.detail}</small></div>)}
    </section>
    <EnterprisePanel enterprise={result.data.enterprise} />
    <ComputeNodesPanel computeNodes={result.data.compute_nodes} />
    <div className="control-grid">
      {result.data.projects.map((project) => <section className="control-card" key={project.project_id}>
        <div className="control-row"><h2>{project.project.name || shortId(project.project_id)}</h2><StatusTag status={project.project_status} /></div>
        <p className="control-muted">Current goal</p>
        {project.current_goal ? <div className="control-row"><strong>{project.current_goal.objective}</strong><StatusTag status={project.current_goal.status} /></div> : <p className="control-muted">No active goal in PADOS authority.</p>}
        <div className="control-row"><strong>Blockers</strong><span>{project.blockers.length}</span></div>
        {project.blockers.slice(0, 3).map((item) => <div className="control-row" key={item.id}><span>{item.description}</span><StatusTag status={item.severity} /></div>)}
        <div className="control-row"><strong>Next decisions</strong><span>{project.next_decisions.length}</span></div>
        {project.next_decisions.slice(0, 3).map((item) => <div className="control-row" key={item.id}>{item.question}</div>)}
        <div className="control-row"><strong>Active runs</strong><span>{project.active_runs.length}</span></div>
        {project.active_runs.slice(0, 3).map((item) => <div className="control-row" key={item.id}><span>{item.name || shortId(item.id)}</span><StatusTag status={item.status} /></div>)}
        <div className="control-row"><strong>Recent accepted results</strong><span>{project.recent_accepted_results.length}</span></div>
        {project.recent_accepted_results.slice(0, 3).map((item) => <div className="control-row" key={item.id}><span>{item.title || shortId(item.id)}</span><small>Scope {item.accepted_scope_version}</small></div>)}
      </section>)}
    </div>
    {result.data.projects.length === 0 && <StatePanel loading={false} error={null} empty="No registered projects are available from PADOS." />}
  </>;
}

function DecisionInboxPage(): ReactElement {
  const result = useJson<DecisionInbox>("/api/decision-inbox");
  if (result.loading || result.error || !result.data) return <StatePanel loading={result.loading} error={result.error} />;
  return <>
    <Header title="Decision / Action Inbox" detail={`${result.data.count} human actions derived from project, run, and decision state`} />
    {result.data.items.length === 0 ? <StatePanel loading={false} error={null} empty="No pending human actions in PADOS authority." /> : <section className="control-card"><div className="control-table-wrap"><table className="control-table"><thead><tr><th>Project</th><th>Item</th><th>Type</th><th>Priority</th><th>Status</th><th>Reason / source</th><th>Required action</th><th>Target</th></tr></thead><tbody>{result.data.items.map((item) => <tr key={item.id}><td>{item.project}</td><td>{item.item}</td><td>{item.type}</td><td>{item.priority}</td><td><StatusTag status={item.status} /></td><td>{item.reason_source}</td><td>{item.required_human_action}</td><td><a href={item.target}>Open</a></td></tr>)}</tbody></table></div></section>}
  </>;
}

function Metric({ label, value }: { label: string; value: ReactElement | string | number }): ReactElement {
  return <div className="control-metric"><span>{label}</span><strong>{value}</strong></div>;
}

function ProjectsPage(): ReactElement {
  const [query, setQuery] = useState("");
  const result = useJson<{ projects: Project[]; pagination: { total: number } }>(`/api/projects${queryString({ q: query, page_size: 100 })}`);
  if (result.loading || result.error || !result.data) return <StatePanel loading={result.loading} error={result.error} />;
  return <><Header title="Projects" detail={`${result.data.pagination.total} registered projects`} /><section className="control-card"><input className="control-input" aria-label="Search projects" placeholder="Search projects" value={query} onChange={(event) => setQuery(event.target.value)} /><div className="control-table-wrap"><table className="control-table"><thead><tr><th>Project</th><th>Status</th><th>Memory</th><th>Tasks</th><th>Runs</th><th>Updated</th></tr></thead><tbody>{result.data.projects.map((project) => <tr key={project.id}><td><strong>{project.name || project.slug || shortId(project.id)}</strong><small>{project.root_path || "No registered root"}</small></td><td><StatusTag status={project.status} /></td><td>{project.memory_count ?? 0}</td><td>{project.task_count ?? 0}</td><td>{project.run_count ?? 0}</td><td>{formatDate(project.updated_at)}</td></tr>)}</tbody></table></div></section></>;
}

function MemoryPage(): ReactElement {
  const [query, setQuery] = useState("");
  const result = useJson<{ memories: ControlMemory[]; pagination: { total: number } }>(`/api/memories${queryString({ q: query, page_size: 25 })}`);
  if (result.loading || result.error || !result.data) return <StatePanel loading={result.loading} error={result.error} />;
  return <><Header title="Memory Explorer" detail="Search governed memory and inspect provenance." /><section className="control-card"><input className="control-input" aria-label="Search memory" placeholder="Search title or content" value={query} onChange={(event) => setQuery(event.target.value)} /><div className="control-table-wrap"><table className="control-table"><thead><tr><th>Memory</th><th>Verification</th><th>Freshness</th><th>Importance</th><th>Source</th></tr></thead><tbody>{result.data.memories.map((memory) => <tr key={memory.memory_id}><td><strong>{memory.title}</strong><small>{memory.content_summary}</small></td><td><StatusTag status={memory.verification_status} /></td><td><StatusTag status={memory.freshness_status} /></td><td>{memory.importance}</td><td>{memory.source_type}</td></tr>)}</tbody></table></div></section></>;
}

function DatabasePage(): ReactElement {
  const overview = useJson<ControlDatabaseOverview>("/api/database/overview");
  const [table, setTable] = useState("projects");
  const rows = useJson<ControlTableRows>(`/api/database/tables/public/${encodeURIComponent(table)}/rows?page_size=25`);
  if (overview.loading || overview.error || !overview.data) return <StatePanel loading={overview.loading} error={overview.error} />;
  return <><Header title="Database Explorer" detail="Metadata and bounded row previews only." /><div className="control-database"><section className="control-card"><h2>Tables</h2>{overview.data.tables.map((item) => <button className={`control-table-button ${item.name === table ? "selected" : ""}`} key={item.name} onClick={() => setTable(item.name)}>{item.name}<small>{item.estimated_row_count ?? "view"}</small></button>)}</section><section className="control-card"><h2>{table}</h2>{rows.loading || rows.error || !rows.data ? <StatePanel loading={rows.loading} error={rows.error} /> : <div className="control-table-wrap"><table className="control-table"><thead><tr>{rows.data.table.columns.map((column) => <th key={column.name}>{column.name}</th>)}</tr></thead><tbody>{rows.data.rows.map((row, index) => <tr key={index}>{rows.data?.table.columns.map((column) => <td key={column.name}>{formatCell(row[column.name])}</td>)}</tr>)}</tbody></table></div>}</section></div></>;
}

function ActivityPage(): ReactElement {
  const runs = useJson<{ runs: ControlActivity[] }>("/api/runs?page_size=100");
  const events = useJson<{ events: ControlActivity[] }>("/api/events?page_size=100");
  if (runs.loading || events.loading || runs.error || events.error || !runs.data || !events.data) return <StatePanel loading={runs.loading || events.loading} error={runs.error || events.error} />;
  const rows = [...runs.data.runs, ...events.data.events];
  return <><Header title="Runs & Events" detail={`${rows.length} append-only activity records`} /><section className="control-card"><div className="control-table-wrap"><table className="control-table"><thead><tr><th>Status</th><th>Name</th><th>Time</th><th>Source</th></tr></thead><tbody>{rows.map((row) => <tr key={`${row.source}-${row.id}`}><td><StatusTag status={row.status} /></td><td>{row.name}</td><td>{formatDate(row.timestamp)}</td><td>{row.source}</td></tr>)}</tbody></table></div></section></>;
}

function SystemPage(): ReactElement {
  const result = useJson<Record<string, unknown>>("/api/system");
  if (result.loading || result.error || !result.data) return <StatePanel loading={result.loading} error={result.error} />;
  return <><Header title="System" detail="Runtime capabilities with protected values omitted." /><section className="control-card"><dl className="control-details">{Object.entries(result.data).map(([key, value]) => <div key={key}><dt>{key.replaceAll("_", " ")}</dt><dd>{formatCell(value)}</dd></div>)}</dl></section></>;
}

function Header({ title, detail }: { title: string; detail: string }): ReactElement {
  return <div className="control-heading"><div><span className="control-eyebrow">Local runtime</span><h1>{title}</h1><p>{detail}</p></div></div>;
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function ControlCenter({ onBack }: { onBack: () => void }): ReactElement {
  const [page, setPage] = useState<Page>("mission");
  const content = page === "mission" ? <MissionControlPage /> : page === "inbox" ? <DecisionInboxPage /> : page === "dashboard" ? <DashboardPage /> : page === "projects" ? <ProjectsPage /> : page === "memory" ? <MemoryPage /> : page === "database" ? <DatabasePage /> : page === "runs" ? <ActivityPage /> : <SystemPage />;
  return <div className="control-shell"><aside className="control-sidebar"><div className="control-brand"><strong>PADOS</strong><span>Local Control Center</span></div><nav aria-label="Control center navigation">{pages.map((item) => <button className={page === item.id ? "active" : ""} key={item.id} onClick={() => setPage(item.id)}>{item.label}</button>)}</nav><button className="control-back" onClick={onBack}>← Corpus Operator</button><p className="control-muted">Local-only, read-only surface.</p></aside><main className="control-main">{content}</main></div>;
}
