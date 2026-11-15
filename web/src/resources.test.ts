// SPDX-License-Identifier: AGPL-3.0-only
// 資源與儲存: values are shown only when measured (UNKNOWN otherwise, with the reason), estimates are labelled, the budget
// level follows the backend's pressure, and the page never offers to delete anything.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { LocaleContext } from "./i18n";
import { ResourcesView } from "./resources-page";
import {
  budgetLevel, formatBytes, formatMetric, metricGroups, sparkline, type Budget, type ResourceMetric, type ResourcesSummary,
} from "./resources";

const AT = "2026-11-16T12:00:00+00:00";
const m = (id: string, value: number | null, unit = "bytes", extra: Partial<ResourceMetric> = {}): ResourceMetric => ({
  id, value, unit, source: `source of ${id}`, measured: value !== null, measured_at: value === null ? null : AT,
  status: value === null ? "UNKNOWN" : "MEASURED", reason: value === null ? "MODEL_RUNTIME_NOT_CONFIGURED" : null, estimate: false, detail: {}, ...extra,
});
const budget = (over: Partial<Budget> = {}): Budget => ({
  source: "pados.lifecycle.storage_policy", policy_status: "DEFAULTS", measured_at: AT, budget_configured: false, local_budget_bytes: null,
  local_bytes: 2048, used_fraction: null, warning_at_used_fraction: 0.85, critical_at_used_fraction: 0.95, min_free_bytes: 0, pressure: "NORMAL",
  pressure_basis: "MEASURED", advice: [], disk: { measured: true, total_bytes: 1000, free_bytes: 500, warning_at_free_fraction: 0.15, critical_at_free_fraction: 0.05 }, ...over,
});

function summary(over: Partial<ResourcesSummary> = {}): ResourcesSummary {
  return {
    schema: "pados.control.resources.v1", authority: "PROJECTION_ONLY", generated_at: AT,
    metrics: [m("ram_in_use", 8 * 1024 ** 3, "bytes", { detail: { total_bytes: 16 * 1024 ** 3, used_fraction: 0.5 } }), m("model_resident_memory", null),
      m("database_size", 50 * 1024 ** 2, "bytes", { detail: { databases: [{ name: "pados_ops", bytes: 50 * 1024 ** 2 }] } }), m("token_usage", null, "tokens", { reason: "NO_MEASURED_MODEL_EXECUTION" }),
      m("job_execution_time", 12.5, "seconds"), m("reclaimable_estimate", 4096, "bytes", { estimate: true }), m("something_new", 3, "rows")],
    budget: budget(),
    trends: {
      execution_records: { status: "MEASURED", source: "row timestamps", reason: null, series: [{ day: "2026-11-15", count: 0 }, { day: "2026-11-16", count: 4 }], rows_per_day_last_7_days: 0.57 },
      storage_bytes: { status: "UNKNOWN", reason: "HISTORY_NOT_RECORDED", detail: "no samples" },
    },
    cleanup_plan: {
      plan_only: true, deletes: false, authority: "NONE", estimated_reclaimable_bytes: 4096,
      candidates: [{ location_id: "runtime_cache", category: "CACHE", data_kinds: ["CACHE_ENTRY"], bytes: 8192, file_count: 3, reclaimable_bytes: 4096, reclaimable_items: 2 }],
      nothing_due: [], never_candidates: ["EVIDENCE", "DECISIONS", "AUDIT_RECORDS", "UNIQUE_COPIES", "DATABASE_ROWS"],
      protected: [{ location_id: "job_artifacts", category: "ARTIFACTS", data_kinds: ["PROJECT_EVIDENCE"], bytes: 100, file_count: 1, reason: "PROTECTED_EVIDENCE_AND_UNIQUE_COPIES" }],
      execute: { available: false, reason: "NO_GOVERNED_REVERSIBLE_CLEANUP_PATH", guidance: "OWNER_AUTHORIZATION_REQUIRED" },
    },
    ...over,
  };
}

describe("resource view logic", () => {
  it("formats only measured values; UNKNOWN is never shown as zero", () => {
    expect(formatMetric(m("ram_in_use", 8 * 1024 ** 3))).toBe("8.0 GiB");
    expect(formatMetric(m("model_resident_memory", null))).toBe("UNKNOWN");
    expect(formatMetric(m("x", 0))).toBe("0 B");
    expect(formatMetric(m("reclaimable_estimate", 2048, "bytes", { estimate: true }))).toBe("≈ 2.0 KiB");
    expect(formatMetric(m("job_execution_time", 125, "seconds"))).toBe("2.1 min");
    expect(formatMetric(m("token_usage", 1500, "tokens"))).toBe("1,500 tokens");
    expect(formatBytes(null)).toBe("—");
  });

  it("groups metrics and keeps unknown ids instead of dropping them", () => {
    const groups = metricGroups(summary().metrics);
    expect(groups.map((g) => g.key)).toEqual(["res.group.memory", "res.group.storage", "res.group.running", "res.group.other"]);
    expect(groups[3].metrics.map((x) => x.id)).toEqual(["something_new"]);
  });

  it("derives the budget level from the backend's pressure", () => {
    expect(budgetLevel(budget())).toBe("ok");
    expect(budgetLevel(budget({ pressure: "PRESSURED" }))).toBe("warning");
    expect(budgetLevel(budget({ pressure: "CRITICAL" }))).toBe("critical");
    expect(budgetLevel(budget({ pressure: "PRESSURED", disk: { ...budget().disk, measured: false, total_bytes: null, free_bytes: null } }))).toBe("unknown");
    expect(budgetLevel(null)).toBe("unknown");
  });

  it("scales the growth sparkline and keeps an empty series flat", () => {
    expect(sparkline([{ day: "a", count: 2 }, { day: "b", count: 4 }]).map((p) => p.height)).toEqual([0.5, 1]);
    expect(sparkline([{ day: "a", count: 0 }]).map((p) => p.height)).toEqual([0]);
  });
});

function render(data: ResourcesSummary, locale: "zh-TW" | "en" = "zh-TW"): string {
  return renderToStaticMarkup(createElement(LocaleContext.Provider, { value: locale }, createElement(ResourcesView, { summary: data })));
}

describe("resources page states", () => {
  it("shows each metric with source, unit and time, and UNKNOWN with its reason", () => {
    const html = render(summary());
    expect(html).toContain('data-testid="metric-ram_in_use" data-status="MEASURED"');
    expect(html).toContain('data-testid="metric-model_resident_memory" data-status="UNKNOWN"');
    expect(html).toContain("MODEL_RUNTIME_NOT_CONFIGURED");
    expect(html).toContain("NO_MEASURED_MODEL_EXECUTION");
    expect(html).toContain("source of ram_in_use · bytes");
    expect(html).toContain("pados_ops · 50.0 MiB");
    expect(html).toContain("估計值");
    expect(html).toContain("模型常駐記憶體");
  });

  it("shows the budget, the measured trend and the storage trend as UNKNOWN", () => {
    const html = render(summary());
    expect(html).toContain('data-testid="budget-not-configured"');
    expect(html).toContain("可用低於 15% 時警告，低於 5% 時為嚴重");
    expect(html).toContain('data-level="ok"');
    expect(html).toContain("<rect");
    expect(html).toContain('data-testid="trend-storage-unknown"');
    expect(html).toContain("HISTORY_NOT_RECORDED");
    const configured = render(summary({ budget: budget({ budget_configured: true, local_budget_bytes: 1000, local_bytes: 900, used_fraction: 0.9, pressure: "PRESSURED" }) }), "en");
    expect(configured).toContain("900 B of 1000 B (90%)");
    expect(configured).toContain("Warning at 85% used, critical at 95% used");
    expect(configured).toContain('data-level="warning"');
    const noTrend = render(summary({ trends: { ...summary().trends, execution_records: { status: "UNKNOWN", source: "x", reason: "DB_NOT_AVAILABLE:OperationalError", series: [], rows_per_day_last_7_days: null } } }));
    expect(noTrend).toContain('data-testid="trend-records-unknown"');
  });

  it("previews cleanup, lists what is protected and offers no delete action", () => {
    const html = render(summary());
    expect(html).toContain('data-testid="cleanup-candidates"');
    expect(html).toContain("runtime_cache");
    expect(html).toContain("PROTECTED_EVIDENCE_AND_UNIQUE_COPIES");
    expect(html).toContain("需要 Owner 授權");
    expect(html).toContain("Evidence、決策、稽核紀錄與唯一副本永遠不會被列為清理對象");
    expect(html).not.toMatch(/<button[^>]*>[^<]*(刪除|清理|Delete|Clean)/);
    const none = render(summary({ cleanup_plan: { ...summary().cleanup_plan, candidates: [], estimated_reclaimable_bytes: 0 } }), "en");
    expect(none).toContain("Nothing is past its retention window.");
  });
});
