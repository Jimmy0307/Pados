// SPDX-License-Identifier: AGPL-3.0-only
// AI 模型與連線 (Providers): the two status levels stay separate, closed codes read as plain words, the edit form only
// produces a non-sensitive update, and the client calls the /control/providers routes with the session or the token.
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CONTROL_TOKEN_HEADER, CSRF_HEADER, type SessionAuth } from "./control-auth";
import { LocaleContext, translate } from "./i18n";
import { diagnoseProvider, listProviders, toggleProvider, updateProvider, type Provider } from "./providers-api";
import { ProviderCard, ProvidersPage } from "./providers-page";
import { codeKey, formFrom, governedSummary, reachKey, shortDigest, updateFrom } from "./providers-view";

const session: SessionAuth = { kind: "session", csrfToken: "csrf-9", deviceId: "dev_9" };

function provider(overrides: Partial<Provider> = {}): Provider {
  return {
    provider_id: "sim-gw", kind: "enterprise_gateway", display_name: "Sim gateway", source: "setup", enabled: true,
    settings: { provider_id: "sim-gw", kind: "enterprise_gateway", display_name: "Sim gateway", enabled: true, endpoint: null,
      contract_path: "/private/gateway.json", timeout_seconds: 30, context_window: null, capability_requirement: "TEXT_SUMMARY",
      project_ids: ["*"], max_classification: "INTERNAL", limits: { max_concurrent_calls: 1, max_output_tokens: 1024 }, auth_env: null },
    auth_env: null, auth_env_present: null,
    reachability: { state: "REACHABLE", code: null, explanation: null },
    models: [
      { model: "sim-small", digest: "sha256:" + "a".repeat(64), loaded: true, governance: "NOT_MEASURED", profile_id: null, reasons: [],
        authorized_by: [], authority: "NON_AUTHORITATIVE_OBSERVATION", explanation: "" },
      { model: "local-measured", digest: "sha256:" + "b".repeat(64), loaded: true, governance: "AUTHORIZED_AND_MEASURED",
        profile_id: "mcp-" + "c".repeat(64), reasons: [], authorized_by: ["lcn-1"], authority: "MEASURED_PROFILE", explanation: "" },
    ],
    checks: [{ check: "health", status: "FAIL", code: "GATEWAY_TLS_FAILED", explanation: null }],
    checked_at: "2026-11-16T00:00:00Z", circuit: { state: "OPEN", consecutive_failures: 3, retry_in_seconds: 12, last_failure_code: "GATEWAY_TIMEOUT" },
    recent_attempts: [],
    gateway: { contract: "OK", base_url: "https://gateway.example.invalid", auth_env: "PADOS_GATEWAY_KEY", auth_env_present: false,
      contract_max_classification: "INTERNAL", contract_execution_enabled: false, idempotency_honoured: false },
    ...overrides,
  };
}

function render(element: ReturnType<typeof createElement>): string {
  return renderToStaticMarkup(createElement(LocaleContext.Provider, { value: "zh-TW" }, element));
}

beforeEach(() => { vi.stubGlobal("location", { hostname: "127.0.0.1" }); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("provider status levels", () => {
  it("keeps reachable and authorized & measured apart", () => {
    expect(translate("zh-TW", reachKey("REACHABLE"))).toBe("已連線 Reachable");
    expect(translate("zh-TW", "providers.gov.AUTHORIZED_AND_MEASURED")).toBe("已授權並測量 Authorized & measured");
    expect(translate("zh-TW", "providers.gov.NOT_MEASURED")).toContain("未授權");
    expect(governedSummary(provider())).toBe("authorized");
    expect(governedSummary(provider({ models: [provider().models[0]] }))).toBe("none");
    expect(governedSummary(provider({ models: [], reachability: { state: "UNREACHABLE", code: "GATEWAY_DNS_FAILED", explanation: null } }))).toBe("unknown");
  });

  it("explains closed codes in plain words with a family fallback", () => {
    expect(translate("zh-TW", codeKey("GATEWAY_NETWORK_UNREACHABLE")!)).toContain("VPN");
    expect(codeKey("VERSION_CONNECTION_FAILED")).toBe("providers.code.runtimeUnreachable");
    expect(codeKey("GATEWAY_CONTRACT_UNREADABLE")).toBe("providers.code.contractInvalid");
    expect(codeKey("SOMETHING_NEW")).toBe("providers.code.unknown");
    expect(codeKey(null)).toBeNull();
    expect(shortDigest("sha256:" + "f".repeat(64))).toBe("sha256:ffffffffffff…");
  });

  it("renders both levels, the probe guardrail and expandable codes", () => {
    const html = render(createElement(ProviderCard, { auth: session, provider: provider(), revision: 3, onChanged: () => undefined }));
    expect(html).toContain("第一層：連線狀態");
    expect(html).toContain("已連線 Reachable");
    expect(html).toContain("第二層：治理狀態");
    expect(html).toContain("至少一個模型可以執行受治理的工作");
    expect(html).toContain("未授權（尚未測量）");
    expect(html).toContain("已授權並測量 Authorized &amp; measured");
    expect(html).toContain("非權威觀察結果");
    expect(html).toContain("請勿在此貼上任何內部文件");
    expect(html).toContain("安全連線失敗");
    expect(html).toContain("<code>GATEWAY_TLS_FAILED</code>");
    expect(html).toContain("開啟：連續失敗後暫停呼叫");
    expect(html).toContain("PADOS_GATEWAY_KEY");
    expect(html).toContain("尚未在這台電腦上設定");
  });

  it("asks for access before reading and states the governed-use rule", () => {
    expect(render(createElement(ProvidersPage, { auth: "" }))).toContain("請先在上方配對這個瀏覽器");
    expect(render(createElement(ProvidersPage, { auth: session }))).toContain("可連線 ≠ 可用於受治理的工作");
  });
});

describe("provider edit form", () => {
  it("round-trips the settings and never carries a key value", () => {
    const form = formFrom(provider().settings);
    const result = updateFrom("enterprise_gateway", { ...form, project_ids: "p1, p2 *", timeout_seconds: "45" });
    expect(result).toEqual({ update: expect.objectContaining({ kind: "enterprise_gateway", endpoint: null, contract_path: "/private/gateway.json",
      timeout_seconds: 45, project_ids: ["p1", "p2", "*"], auth_env: null }) });
  });

  it("refuses what the server would refuse", () => {
    const form = formFrom(provider().settings);
    expect(updateFrom("enterprise_gateway", { ...form, timeout_seconds: "0" })).toEqual({ error: "providers.form.timeoutInvalid" });
    expect(updateFrom("enterprise_gateway", { ...form, max_classification: "RESTRICTED" })).toEqual({ error: "providers.form.restricted" });
    expect(updateFrom("enterprise_gateway", { ...form, auth_env: "PADOS_KEY" })).toEqual({ error: "providers.form.authEnvInvalid" });
    expect(updateFrom("ollama", { ...form, endpoint: "" })).toEqual({ error: "providers.form.endpointRequired" });
    expect(updateFrom("ollama", { ...form, endpoint: "http://127.0.0.1:11434", auth_env: "lower" })).toEqual({ error: "providers.form.authEnvInvalid" });
  });
});

describe("providers client", () => {
  function router(): { sent: Array<{ url: string; init: RequestInit }>; fetcher: typeof fetch } {
    const sent: Array<{ url: string; init: RequestInit }> = [];
    const fetcher = (async (url: string, init: RequestInit) => {
      sent.push({ url, init });
      return new Response(JSON.stringify({ provider: provider(), setup_revision: 4, providers: [], setup_revision_: 0 }), { status: 200, headers: { "Content-Type": "application/json" } });
    }) as unknown as typeof fetch;
    return { sent, fetcher };
  }

  it("calls the control routes with the session (CSRF on writes only) or the token", async () => {
    const { sent, fetcher } = router();
    await listProviders(session, fetcher);
    await diagnoseProvider(session, "sim-gw", true, fetcher);
    await toggleProvider(session, "sim-gw", false, 3, fetcher);
    const result = updateFrom("enterprise_gateway", formFrom(provider().settings));
    if (!("update" in result)) throw new Error("the form must be valid");
    await updateProvider("operator-typed-value", "sim-gw", result.update, 4, fetcher);
    expect(sent.map((item) => [item.init.method || "GET", item.url])).toEqual([
      ["GET", "/control/providers"], ["POST", "/control/providers/sim-gw/diagnostics"], ["POST", "/control/providers/sim-gw/disable"],
      ["PUT", "/control/providers/sim-gw"]]);
    const headers = (index: number) => sent[index].init.headers as Record<string, string>;
    expect(headers(0)[CSRF_HEADER]).toBeUndefined();
    expect(headers(1)[CSRF_HEADER]).toBe("csrf-9");
    expect(JSON.parse(String(sent[1].init.body))).toEqual({ probe_chat: true });
    expect(JSON.parse(String(sent[2].init.body))).toEqual({ expected_revision: 3 });
    expect(headers(3)[CONTROL_TOKEN_HEADER]).toBe("operator-typed-value");
    expect(JSON.parse(String(sent[3].init.body)).expected_revision).toBe(4);
  });
});
