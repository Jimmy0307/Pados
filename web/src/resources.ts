// SPDX-License-Identifier: AGPL-3.0-only
// 資源與儲存 (Resources): pure logic over GET /control/resources. A metric is shown as a number only when the backend
// measured it; otherwise it is UNKNOWN with the backend's reason. Estimates are labelled. The cleanup plan is a preview
// the backend computed: this page never offers to delete anything (the backend reports no governed, reversible path).
import type { MessageKey } from "./i18n";

export type ResourceMetric = {
  id: string; value: number | null; unit: string; source: string; measured: boolean; measured_at: string | null;
  status: "MEASURED" | "UNKNOWN"; reason: string | null; estimate: boolean; detail: Record<string, unknown>;
};
export type Budget = {
  source: string; policy_status: string; measured_at: string; budget_configured: boolean; local_budget_bytes: number | null;
  local_bytes: number | null; used_fraction: number | null; warning_at_used_fraction: number; critical_at_used_fraction: number;
  min_free_bytes: number; pressure: "NORMAL" | "PRESSURED" | "CRITICAL"; pressure_basis: string; advice: string[];
  disk: { measured: boolean; total_bytes: number | null; free_bytes: number | null; warning_at_free_fraction: number; critical_at_free_fraction: number };
};
export type SeriesPoint = { day: string; count: number };
export type CleanupEntry = { location_id: string; category: string; data_kinds: string[]; bytes: number; file_count: number; reason?: string; reclaimable_bytes?: number; reclaimable_items?: number; oldest_mtime?: string | null };
export type CleanupPlan = {
  plan_only: boolean; deletes: boolean; authority: string; estimated_reclaimable_bytes: number;
  candidates: CleanupEntry[]; nothing_due: CleanupEntry[]; protected: CleanupEntry[]; never_candidates: string[];
  execute: { available: boolean; reason: string; guidance: string };
};
export type ResourcesSummary = {
  schema: string; authority: string; generated_at: string; metrics: ResourceMetric[]; budget: Budget;
  trends: {
    execution_records: { status: string; source: string; reason: string | null; series: SeriesPoint[]; rows_per_day_last_7_days: number | null };
    storage_bytes: { status: string; reason: string; detail: string };
  };
  cleanup_plan: CleanupPlan;
};

export type MetricGroup = { key: MessageKey; metrics: ResourceMetric[] };

const GROUPS: Array<{ key: MessageKey; ids: string[] }> = [
  { key: "res.group.memory", ids: ["ram_in_use", "pados_process_rss", "model_resident_memory"] },
  { key: "res.group.storage", ids: ["database_size", "database_index_size", "project_documents", "cache_size", "evidence_files", "operational_logs", "reclaimable_estimate"] },
  { key: "res.group.running", ids: ["execution_records", "token_usage", "model_execution_time", "job_execution_time"] },
];

/** Metrics in their page groups (memory, storage, running cost); anything new lands in "other", never dropped. */
export function metricGroups(metrics: ResourceMetric[]): MetricGroup[] {
  const known = new Set(GROUPS.flatMap((group) => group.ids));
  const groups = GROUPS.map((group) => ({ key: group.key, metrics: group.ids.flatMap((id) => metrics.filter((m) => m.id === id)) }));
  const other = metrics.filter((metric) => !known.has(metric.id));
  return [...groups, ...(other.length ? [{ key: "res.group.other" as MessageKey, metrics: other }] : [])].filter((g) => g.metrics.length);
}

const UNITS = ["B", "KiB", "MiB", "GiB", "TiB"];

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  let size = Math.max(0, value);
  let unit = 0;
  while (size >= 1024 && unit < UNITS.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? size : size.toFixed(size >= 100 ? 0 : 1)} ${UNITS[unit]}`;
}

export function formatSeconds(value: number): string {
  if (value < 60) return `${value.toFixed(value < 10 ? 2 : 1)} s`;
  if (value < 3600) return `${(value / 60).toFixed(1)} min`;
  return `${(value / 3600).toFixed(1)} h`;
}

/** The value as shown: UNKNOWN when not measured, never 0 in its place. */
export function formatMetric(metric: ResourceMetric): string {
  if (!metric.measured || metric.value === null) return "UNKNOWN";
  const prefix = metric.estimate ? "≈ " : "";
  if (metric.unit === "bytes") return prefix + formatBytes(metric.value);
  if (metric.unit === "seconds") return prefix + formatSeconds(metric.value);
  return `${prefix}${metric.value.toLocaleString("en-US")} ${metric.unit}`;
}

export type Level = "ok" | "warning" | "critical" | "unknown";

/** The budget's level from the backend's pressure; free disk space that could not be measured is "unknown". */
export function budgetLevel(budget: Budget | null | undefined): Level {
  if (!budget) return "unknown";
  if (budget.pressure === "CRITICAL") return "critical";
  if (!budget.disk.measured && !budget.budget_configured) return "unknown";
  return budget.pressure === "PRESSURED" ? "warning" : "ok";
}

export const percent = (fraction: number | null | undefined): string =>
  fraction === null || fraction === undefined ? "—" : String(Math.round(fraction * 1000) / 10);

/** Bar heights (0..1) for the growth sparkline; an all-zero series stays flat. */
export function sparkline(series: SeriesPoint[]): Array<SeriesPoint & { height: number }> {
  const top = Math.max(0, ...series.map((point) => point.count));
  return series.map((point) => ({ ...point, height: top ? point.count / top : 0 }));
}

export function metricLabelKey(id: string): MessageKey | null {
  const key = `res.metric.${id}`;
  return GROUPS.some((group) => group.ids.includes(id)) ? (key as MessageKey) : null;
}
