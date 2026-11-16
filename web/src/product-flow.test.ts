// SPDX-License-Identifier: AGPL-3.0-only
// The product work loop's pure logic: the deterministic plan, the captured root goal, the probe's evidence, the run
// phase, the decision state (never "accepted" without a recorded APPROVE), and plain-language failures.
import { describe, expect, it } from "vitest";
import { HttpError } from "./http";
import { DEFAULT_LOCALE, englishKeys, missingTranslations, translate } from "./i18n";
import {
  CAPABILITY_OPTIONS, OPERATIONS, buildPlanTemplate, canMakePlan, candidateState, failureView, isStopped, loopPhase, markdownBlocks,
  parseProbeReport, rootGoalId, stepUpState,
} from "./product-flow";

describe("i18n", () => {
  it("defaults to Traditional Chinese and falls back to English per key", () => {
    expect(DEFAULT_LOCALE).toBe("zh-TW");
    expect(translate("zh-TW", "work.submit")).toBe("提交候選結果");
    expect(translate("en", "work.submit")).toBe("Submit result candidate");
    expect(translate("zh-TW", "lang.switch")).toBe("中文 / English");  // not translated: English fallback
    expect(translate("zh-TW", "projects.created", { name: "示範" })).toBe("已建立專案 示範。");
    expect(translate("en", "work.run.progress", { current: 1 })).toBe("Iteration 1 of at most {max}");
  });

  it("translates every product message except the language switch", () => {
    expect(missingTranslations("zh-TW")).toEqual(["lang.switch"]);
    expect(englishKeys().length).toBeGreaterThan(100);
    // The web boundary lint's forbidden words, built from fragments so this test itself stays lint-clean.
    const forbidden = new RegExp(["sec" + "ret", "pass" + "word", "creden" + "tial"].join("|"), "i");
    for (const key of englishKeys()) {
      expect(translate("zh-TW", key)).not.toMatch(forbidden);
      expect(translate("en", key)).not.toMatch(forbidden);
    }
  });

  it("labels the template plan as deterministic and AI-free in both languages", () => {
    expect(translate("zh-TW", "work.plan.source")).toContain("未使用 AI");
    expect(translate("en", "work.plan.source")).toBe("Deterministic template, no AI");
  });
});

describe("plan template", () => {
  const plan = buildPlanTemplate({ projectId: "p1", requestId: "r1", rootGoalId: "goal-root:abc", title: "檢查文件連結", text: "請檢查連結" });

  it("authorizes exactly one read-only markdown_reference_check step bound to the root goal", () => {
    expect(plan.source).toBe("DETERMINISTIC_TEMPLATE");
    expect(plan.subject).toEqual({ kind: "WORK_ROUTE", schema_version: "pados.approval-subject.v1", project_id: "p1", request_id: "r1",
      actor: { surfaces: ["TASK_LOOP"], worker_types: ["DETERMINISTIC_TOOL"] }, operations: OPERATIONS, scope_version_ref: null, task_envelope_hash: null });
    expect(OPERATIONS).toEqual([{ step_sequence: 1, capability_id: "RUN_STATIC_ANALYSIS", command_id: "markdown_reference_check", max_risk_level: "LOW" }]);
    expect(plan.steps).toHaveLength(1);
    expect(plan.steps[0]).toMatchObject({ sequence: 1, product_goal_id: "goal-root:abc", required_capability: "RUN_STATIC_ANALYSIS",
      command_id: "markdown_reference_check", output_contract: { pados_capability_outcome: "pados.capability-outcome.v1" } });
    expect(plan.risk).toBe("LOW");
    expect(plan.prohibitedActions).toEqual(["merge", "push", "deploy"]);
    expect(plan.proposalSummary).toMatch(/^Deterministic template \(no AI\)/);
    expect(plan.goal.objective.length).toBeLessThanOrEqual(1000);
  });

  it("offers only the deterministic check as runnable", () => {
    expect(CAPABILITY_OPTIONS.filter((o) => o.available).map((o) => o.id)).toEqual(["RUN_STATIC_ANALYSIS"]);
  });

  it("reads the captured envelope's root goal and nothing else", () => {
    const body = JSON.stringify({ request_text: "x", task_envelope: { normalized_goals: [{ goal_id: "goal-root:1" }] } });
    expect(rootGoalId(body)).toBe("goal-root:1");
    expect(rootGoalId("plain text")).toBeNull();
    expect(rootGoalId(JSON.stringify({ task_envelope: {} }))).toBeNull();
    expect(rootGoalId(undefined)).toBeNull();
  });
});

describe("evidence and states", () => {
  it("parses the probe report and its broken links", () => {
    const report = parseProbeReport(JSON.stringify({ files_checked: 2, references_checked: 2, broken: [{ file: "guide.md", target: "missing.md" }, { bad: 1 }] }));
    expect(report).toEqual({ filesChecked: 2, referencesChecked: 2, broken: [{ file: "guide.md", target: "missing.md" }], error: null });
    expect(parseProbeReport("not json")?.error).toBe("REPORT_UNREADABLE");
    expect(parseProbeReport("")).toBeNull();
  });

  it("maps loop statuses to phases and stops stepping when the loop waits", () => {
    expect(loopPhase("RUNNING")).toBe("running");
    expect(loopPhase("WAITING_HUMAN")).toBe("waiting_human");
    expect(loopPhase("FAILED_TERMINAL")).toBe("failed");
    expect(loopPhase("COMPLETED")).toBe("completed");
    expect(isStopped("RUNNING")).toBe(false);
    expect(isStopped("READY")).toBe(false);
    for (const status of ["COMPLETED", "WAITING_HUMAN", "PAUSED", "BLOCKED", "CANCELLED", "TIMED_OUT"]) expect(isStopped(status)).toBe(true);
  });

  it("never reports acceptance without a recorded APPROVE decision", () => {
    expect(candidateState(null)).toBeNull();
    expect(candidateState({ verification_status: "PASSED", approval_status: "PENDING", decision: null })).toBe("WAITING_STEP_UP");
    expect(candidateState({ verification_status: "PASSED", approval_status: "APPROVED", decision: null })).toBe("WAITING_STEP_UP");
    expect(candidateState({ verification_status: "PASSED", decision: "APPROVE" })).toBe("ACCEPTED");
    expect(candidateState({ verification_status: "PASSED", decision: "REJECT" })).toBe("REJECTED");
    expect(candidateState({ verification_status: "FAILED", decision: "APPROVE" })).toBe("VERIFICATION_FAILED");
  });

  it("explains the step-up readiness", () => {
    expect(stepUpState(undefined)).toBe("remote_not_configured");
    expect(stepUpState({ remote_console: "NOT_CONFIGURED" })).toBe("remote_not_configured");
    expect(stepUpState({ remote_console: "CONFIGURED", presence_device: "NOT_ENROLLED" })).toBe("no_presence_device");
    expect(stepUpState({ remote_console: "CONFIGURED", presence_device: "ENROLLED" })).toBe("ready");
  });

  it("turns refusals into plain words and keeps the technical code", () => {
    const stepUp = new HttpError("SERVER_ERROR", 403, "RESULT_ACCEPTANCE_REQUIRES_STEP_UP", "req-1", false, "NO");
    expect(failureView(stepUp)).toEqual({ key: "error.RESULT_ACCEPTANCE_REQUIRES_STEP_UP", code: "RESULT_ACCEPTANCE_REQUIRES_STEP_UP", requestId: "req-1", status: 403 });
    const transition = new HttpError("SERVER_ERROR", 409, "LOOP_TRANSITION_REFUSED: the loop is unknown", "r", false, "NO");
    expect(failureView(transition).key).toBe("error.LOOP_TRANSITION_REFUSED");
    expect(failureView(new HttpError("SESSION_STALE", 403, "CSRF_INVALID", "r", false, "NO")).key).toBe("error.SESSION");
    expect(failureView(new HttpError("WRITE_OUTCOME_UNKNOWN", 0, "WRITE_OUTCOME_UNKNOWN", "r", false, "UNKNOWN")).key).toBe("error.WRITE_OUTCOME_UNKNOWN");
    expect(failureView(new HttpError("SERVER_ERROR", 500, "SOMETHING_NEW", "r", false, "NO")).key).toBe("error.generic");
    expect(failureView(new Error("boom")).code).toBe("boom");
  });
});

describe("markdown preview", () => {
  it("reads headings, items, code and links as text", () => {
    const blocks = markdownBlocks("# 使用指南\r\n\n請閱讀[說明](README.md) 與 [網站](https://example.com)。\n- [壞](missing.md)\n```\n[not](a link)\n```\n<script>x</script>");
    expect(blocks[0]).toEqual({ kind: "heading", level: 1, parts: [{ kind: "text", text: "使用指南" }] });
    expect(blocks[1]).toEqual({ kind: "paragraph", parts: [
      { kind: "text", text: "請閱讀" }, { kind: "link", text: "說明", target: "README.md", relative: true },
      { kind: "text", text: " 與 " }, { kind: "link", text: "網站", target: "https://example.com", relative: false }, { kind: "text", text: "。" }] });
    expect(blocks[2]).toEqual({ kind: "item", parts: [{ kind: "link", text: "壞", target: "missing.md", relative: true }] });
    expect(blocks[3]).toEqual({ kind: "code", text: "[not](a link)" });
    expect(blocks[4]).toEqual({ kind: "paragraph", parts: [{ kind: "text", text: "<script>x</script>" }] });
  });
});

describe("making a plan", () => {
  const loaded = [{ id: "p-1" }, { id: "p-2" }];
  it("waits for the project list: a chosen project with no loaded option would make the required select empty", () => {
    expect(canMakePlan({ projects: undefined, projectId: "p-1", available: true, busy: false })).toBe(false);
    expect(canMakePlan({ projects: [], projectId: "p-1", available: true, busy: false })).toBe(false);
    expect(canMakePlan({ projects: [{ id: "p-2" }], projectId: "p-1", available: true, busy: false })).toBe(false);
    expect(canMakePlan({ projects: loaded, projectId: "p-1", available: true, busy: false })).toBe(true);
  });

  it("needs a project, an available capability and no request in flight", () => {
    expect(canMakePlan({ projects: loaded, projectId: "", available: true, busy: false })).toBe(false);
    expect(canMakePlan({ projects: loaded, projectId: "p-1", available: false, busy: false })).toBe(false);
    expect(canMakePlan({ projects: loaded, projectId: "p-1", available: true, busy: true })).toBe(false);
  });
});
