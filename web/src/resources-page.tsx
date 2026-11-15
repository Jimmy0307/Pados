// SPDX-License-Identifier: AGPL-3.0-only
// 資源與儲存 (Resources) page: what GET /control/resources measured, each value with its source, unit, time and whether it
// was measured (UNKNOWN otherwise, with the reason), the storage budget with its warning thresholds, the execution-record
// growth trend, and the cleanup plan PREVIEW. There is no delete or cleanup button: PADOS has no governed, reversible
// cleanup path, so the page says that the Owner must authorize any removal.
import type { ReactElement } from "react";
import { formatDate } from "./api";
import { hasControlAuth, type ControlAuth } from "./control-auth";
import { useT } from "./i18n";
import { resourcesSummary } from "./product-api";
import { useLoad } from "./product-load";
import { Blocked, Completed, Disabled, Failed, Loading, Section } from "./product-ui";
import {
  budgetLevel, formatBytes, formatMetric, metricGroups, metricLabelKey, percent, sparkline,
  type Budget, type CleanupPlan, type ResourceMetric, type ResourcesSummary,
} from "./resources";

export function ResourcesPage({ auth, onSessionLost }: { auth: ControlAuth; onSessionLost?: (error: unknown) => void }): ReactElement {
  const t = useT();
  const allowed = hasControlAuth(auth);
  const summary = useLoad(allowed ? () => resourcesSummary(auth) : null, [auth, allowed], onSessionLost);
  if (!allowed) return <Disabled reason={t("auth.required")} />;
  return <>
    <div className="control-heading"><div><span className="control-eyebrow">PADOS</span><h1>{t("res.title")}</h1><p>{t("res.detail")}</p></div>
      <button className="control-button" type="button" disabled={summary.loading} onClick={summary.reload}>{t("res.refresh")}</button></div>
    {summary.loading && !summary.data && <Loading />}
    {Boolean(summary.error) && <Failed error={summary.error} onRetry={summary.reload} />}
    {summary.data && <ResourcesView summary={summary.data} />}
  </>;
}

export function ResourcesView({ summary }: { summary: ResourcesSummary }): ReactElement {
  const t = useT();
  return <div data-testid="resources">
    <p className="control-muted">{t("res.generatedAt", { at: formatDate(summary.generated_at) })} · {summary.authority}</p>
    {metricGroups(summary.metrics).map((group) => <Section key={group.key} title={t(group.key)} testId={`resources-${group.key.split(".").pop()}`}>
      <div className="res-grid">{group.metrics.map((metric) => <MetricCard key={metric.id} metric={metric} />)}</div>
    </Section>)}
    <BudgetSection budget={summary.budget} />
    <TrendSection summary={summary} />
    <CleanupSection plan={summary.cleanup_plan} />
  </div>;
}

function MetricExtra({ metric }: { metric: ResourceMetric }): ReactElement | null {
  const detail = metric.detail || {};
  const databases = Array.isArray(detail.databases) ? detail.databases as Array<{ name: string; bytes: number }> : [];
  const models = Array.isArray(detail.models) ? detail.models as Array<{ name: string; size_bytes: number | null }> : [];
  const locations = Array.isArray(detail.locations) ? detail.locations as Array<{ id: string; bytes: number; files: number }> : [];
  const rows = [...databases.map((d) => `${d.name} · ${formatBytes(d.bytes)}`), ...models.map((m) => `${m.name} · ${formatBytes(m.size_bytes)}`),
    ...locations.map((l) => `${l.id} · ${formatBytes(l.bytes)} · ${l.files} files`)];
  if (typeof detail.total_bytes === "number") rows.push(`total ${formatBytes(detail.total_bytes)} · ${percent(Number(detail.used_fraction))}%`);
  if (typeof detail.prompt_tokens === "number") rows.push(`prompt ${detail.prompt_tokens} · output ${String(detail.output_tokens)}`);
  if (typeof detail.average_seconds === "number") rows.push(`${String(detail.jobs)} jobs · avg ${detail.average_seconds} s`);
  return rows.length ? <ul className="res-extra">{rows.slice(0, 8).map((row) => <li key={row}>{row}</li>)}</ul> : null;
}

function MetricCard({ metric }: { metric: ResourceMetric }): ReactElement {
  const t = useT();
  const label = metricLabelKey(metric.id);
  return <article className={`res-metric res-${metric.measured ? "measured" : "unknown"}`} data-testid={`metric-${metric.id}`} data-status={metric.status}>
    <h3>{label ? t(label) : metric.id}</h3>
    <strong className="res-value">{formatMetric(metric)}</strong>
    <p><span className={`control-status control-status-${metric.measured ? "ok" : "warning"}`}>{metric.measured ? t("res.measured") : t("res.unknown")}</span>
      {metric.estimate && <span className="product-badge res-estimate">{t("res.estimate")}</span>}</p>
    <dl className="res-meta">
      <div><dt>{t("res.source")}</dt><dd>{metric.source} · {metric.unit}</dd></div>
      {metric.measured ? <div><dt>{t("res.measuredAt")}</dt><dd>{formatDate(metric.measured_at || undefined)}</dd></div>
        : <div><dt>{t("res.reason")}</dt><dd><code>{metric.reason}</code></dd></div>}
    </dl>
    <MetricExtra metric={metric} />
  </article>;
}

function BudgetSection({ budget }: { budget: Budget }): ReactElement {
  const t = useT();
  const level = budgetLevel(budget);
  const used = budget.used_fraction;
  const diskFree = budget.disk.measured && budget.disk.total_bytes ? (budget.disk.free_bytes || 0) / budget.disk.total_bytes : null;
  return <Section title={t("res.budget")} testId="resources-budget">
    <div className="control-row"><span>{t("res.budget.pressure")}</span><span data-testid="budget-pressure" data-level={level}>
      <span className={`control-status control-status-${level === "critical" ? "error" : level === "ok" ? "ok" : level === "warning" ? "warning" : "unknown"}`}>{budget.pressure}</span></span></div>
    <div className="control-row"><span>{t("res.budget.policy")}</span><span><code>{budget.policy_status}</code></span></div>
    {budget.budget_configured
      ? <><div className="control-row"><span>{t("res.budget.local")}</span><span>{t("res.budget.used", { used: formatBytes(budget.local_bytes), budget: formatBytes(budget.local_budget_bytes), percent: percent(used) })}</span></div>
        <Meter fraction={used} warning={budget.warning_at_used_fraction} critical={budget.critical_at_used_fraction} />
        <p className="control-muted">{t("res.budget.thresholds", { warning: percent(budget.warning_at_used_fraction), critical: percent(budget.critical_at_used_fraction) })}</p></>
      : <p className="control-muted" data-testid="budget-not-configured">{t("res.budget.notConfigured")} {budget.local_bytes !== null && `${t("res.budget.local")}: ${formatBytes(budget.local_bytes)}`}</p>}
    <div className="control-row"><span>{t("res.budget.disk")}</span><span>{budget.disk.measured ? `${formatBytes(budget.disk.free_bytes)} / ${formatBytes(budget.disk.total_bytes)} (${percent(diskFree)}%)` : "UNKNOWN"}</span></div>
    {!budget.disk.measured && <Blocked message={t("res.budget.diskUnknown")} />}
    <p className="control-muted">{t("res.budget.diskThresholds", { warning: percent(budget.disk.warning_at_free_fraction), critical: percent(budget.disk.critical_at_free_fraction) })}</p>
    {budget.advice.length > 0 && <p className="control-muted">{t("res.budget.advice")}: {budget.advice.join(", ")}</p>}
    <p className="control-muted res-source">{budget.source}</p>
  </Section>;
}

function Meter({ fraction, warning, critical }: { fraction: number | null; warning: number; critical: number }): ReactElement {
  const value = Math.min(1, Math.max(0, fraction || 0));
  const tone = value >= critical ? "critical" : value >= warning ? "warning" : "ok";
  return <svg className="res-meter" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label={`${percent(fraction)}%`}>
    <rect className="res-meter-track" x="0" y="0" width="100" height="8" />
    <rect className={`res-meter-fill res-meter-${tone}`} x="0" y="0" width={value * 100} height="8" />
    <line className="res-meter-mark" x1={warning * 100} x2={warning * 100} y1="0" y2="8" />
    <line className="res-meter-mark res-meter-critical-mark" x1={critical * 100} x2={critical * 100} y1="0" y2="8" />
  </svg>;
}

function TrendSection({ summary }: { summary: ResourcesSummary }): ReactElement {
  const t = useT();
  const records = summary.trends.execution_records;
  const bars = sparkline(records.series || []);
  return <Section title={t("res.trend")} testId="resources-trend">
    {records.status === "MEASURED" ? <>
      <p className="control-muted">{t("res.trend.records", { days: bars.length })}</p>
      <svg className="res-spark" viewBox={`0 0 ${Math.max(1, bars.length) * 10} 40`} preserveAspectRatio="none" role="img" aria-label={t("res.trend.records", { days: bars.length })}>
        {bars.map((bar, index) => <rect key={bar.day} x={index * 10 + 1} y={40 - Math.max(1, bar.height * 38)} width="8" height={Math.max(1, bar.height * 38)}><title>{`${bar.day}: ${bar.count}`}</title></rect>)}
      </svg>
      <p>{t("res.trend.perDay", { count: records.rows_per_day_last_7_days ?? 0 })}</p>
      <p className="control-muted res-source">{records.source}</p>
    </> : <p data-testid="trend-records-unknown">UNKNOWN · <code>{records.reason}</code></p>}
    <p className="res-unknown-note" data-testid="trend-storage-unknown">{t("res.trend.storageUnknown")} <code>{summary.trends.storage_bytes.reason}</code></p>
  </Section>;
}

function CleanupSection({ plan }: { plan: CleanupPlan }): ReactElement {
  const t = useT();
  return <Section title={t("res.cleanup")} testId="resources-cleanup">
    <p className="control-muted">{t("res.cleanup.help")}</p>
    <h3>{t("res.cleanup.candidates")}</h3>
    {plan.candidates.length === 0 ? <Completed message={t("res.cleanup.none")} />
      : <ul className="res-list" data-testid="cleanup-candidates">{plan.candidates.map((c) => <li key={c.location_id}>
        <strong>{c.location_id}</strong> <small>{c.category} · {c.data_kinds.join(", ")}</small>
        <span>≈ {formatBytes(c.reclaimable_bytes)} · {c.reclaimable_items} files</span></li>)}</ul>}
    <p>{t("res.cleanup.total", { size: formatBytes(plan.estimated_reclaimable_bytes) })}</p>
    <h3>{t("res.cleanup.protected")}</h3>
    <p className="control-muted">{t("res.cleanup.never")}</p>
    <ul className="res-list" data-testid="cleanup-protected">{plan.protected.map((p) => <li key={p.location_id}>
      <strong>{p.location_id}</strong> <small>{p.category}</small> <code>{p.reason}</code> <span>{formatBytes(p.bytes)}</span></li>)}</ul>
    {!plan.execute.available && <Blocked message={t("res.cleanup.owner")} next={t("res.cleanup.ownerHelp")} />}
  </Section>;
}
