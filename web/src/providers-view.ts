// SPDX-License-Identifier: AGPL-3.0-only
// Pure view logic of AI 模型與連線 (Providers): the two status levels, plain-language reasons for closed codes, and the
// edit form <-> setup update conversion. Reachable (level 1) and authorized & measured (level 2) are never merged.
import { hasMessage, type MessageKey } from "./i18n";
import type { Classification, Governance, Provider, ProviderSettings, ProviderUpdate, ReachState, Requirement } from "./providers-api";

export const CLASSIFICATIONS: Classification[] = ["PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"];
export const REQUIREMENTS: Requirement[] = ["STRUCTURED_OUTPUT", "TEXT_SUMMARY"];
const ENV_NAME = /^[A-Z][A-Z0-9_]{1,63}$/;

export function reachKey(state: ReachState): MessageKey {
  const key = `providers.reach.${state}`;
  return hasMessage(key) ? key : "providers.reach.NOT_CHECKED";
}

export function governanceKey(governance: Governance): MessageKey {
  const key = `providers.gov.${governance}`;
  return hasMessage(key) ? key : "providers.gov.NOT_MEASURED";
}

export function governanceTone(governance: Governance): "ok" | "warning" | "error" {
  return governance === "AUTHORIZED_AND_MEASURED" ? "ok" : governance === "MEASURED_NOT_AUTHORIZED" ? "warning" : "error";
}

export function reachTone(state: ReachState): "ok" | "warning" | "error" | "unknown" {
  return state === "REACHABLE" ? "ok" : state === "UNREACHABLE" ? "error" : state === "DEGRADED" ? "warning" : "unknown";
}

/** Level 2 for the whole provider: may ANY of its models run governed work? Not measured means not authorized. */
export function governedSummary(provider: Provider): "authorized" | "none" | "unknown" {
  if (!provider.models.length) return provider.reachability.state === "REACHABLE" ? "none" : "unknown";
  return provider.models.some((model) => model.governance === "AUTHORIZED_AND_MEASURED") ? "authorized" : "none";
}

/** A closed code in plain words (zh-TW first): the specific message, a family message, or a generic one. */
export function codeKey(code: string | null | undefined): MessageKey | null {
  if (!code) return null;
  const head = code.split(":")[0].trim();
  const specific = `providers.code.${head}`;
  if (hasMessage(specific)) return specific;
  if (/_CONNECTION_FAILED$|_UNREACHABLE$|^RUNTIME_UNREACHABLE/.test(head)) return "providers.code.runtimeUnreachable";
  if (head.startsWith("PROVIDER_ENDPOINT_MUST_BE_PRIVATE")) return "providers.code.endpointPrivate";
  if (head.startsWith("GATEWAY_CONTRACT") || head.startsWith("GATEWAY_BASE_URL")) return "providers.code.contractInvalid";
  return "providers.code.unknown";
}

export function shortDigest(digest: string | null | undefined): string {
  if (!digest) return "—";
  const hex = digest.replace(/^sha256:/, "");
  return `sha256:${hex.slice(0, 12)}…`;
}

export type ProviderForm = {
  display_name: string; endpoint: string; contract_path: string; timeout_seconds: string; context_window: string;
  capability_requirement: "" | Requirement; project_ids: string; max_classification: Classification;
  max_concurrent_calls: string; max_output_tokens: string; auth_env: string;
};

export function formFrom(settings: ProviderSettings): ProviderForm {
  return {
    display_name: settings.display_name || "", endpoint: settings.endpoint || "", contract_path: settings.contract_path || "",
    timeout_seconds: String(settings.timeout_seconds), context_window: settings.context_window ? String(settings.context_window) : "",
    capability_requirement: settings.capability_requirement || "", project_ids: settings.project_ids.join(", "),
    max_classification: settings.max_classification, max_concurrent_calls: String(settings.limits.max_concurrent_calls),
    max_output_tokens: String(settings.limits.max_output_tokens), auth_env: settings.auth_env || "",
  };
}

function integer(value: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(value.trim())) return null;
  const parsed = Number(value.trim());
  return parsed >= min && parsed <= max ? parsed : null;
}

/** The setup update a form describes, or the message key of the first field the server would refuse. */
export function updateFrom(kind: ProviderSettings["kind"], form: ProviderForm): { update: ProviderUpdate } | { error: MessageKey } {
  const gateway = kind === "enterprise_gateway";
  const timeout = integer(form.timeout_seconds, 1, 600);
  if (timeout === null) return { error: "providers.form.timeoutInvalid" };
  const context = form.context_window.trim() ? integer(form.context_window, 256, 2_000_000) : null;
  if (form.context_window.trim() && context === null) return { error: "providers.form.contextInvalid" };
  const concurrency = integer(form.max_concurrent_calls, 1, 16);
  const output = integer(form.max_output_tokens, 1, 32_768);
  if (concurrency === null || output === null) return { error: "providers.form.limitsInvalid" };
  const env = form.auth_env.trim();
  if (env && (gateway || !ENV_NAME.test(env))) return { error: "providers.form.authEnvInvalid" };
  if (gateway && !form.contract_path.trim()) return { error: "providers.form.contractRequired" };
  if (!gateway && !form.endpoint.trim()) return { error: "providers.form.endpointRequired" };
  if (gateway && form.max_classification === "RESTRICTED") return { error: "providers.form.restricted" };
  return {
    update: {
      kind, display_name: form.display_name.trim(), endpoint: gateway ? null : form.endpoint.trim(),
      contract_path: gateway ? form.contract_path.trim() : null, timeout_seconds: timeout, context_window: context,
      capability_requirement: form.capability_requirement || null,
      project_ids: form.project_ids.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean),
      max_classification: form.max_classification, limits: { max_concurrent_calls: concurrency, max_output_tokens: output },
      auth_env: env || null,
    },
  };
}
