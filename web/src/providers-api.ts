// SPDX-License-Identifier: AGPL-3.0-only
// AI 模型與連線 (Providers): the /control/providers calls, sent with this browser's paired session or the control token.
// The server decides everything; this file only carries requests and the response shapes. No key value ever travels
// here: a provider names the environment variable that holds its key (auth_env), never the value.
import { controlRequest } from "./control-actions";
import type { ControlAuth } from "./control-auth";

type Fetcher = typeof fetch | undefined;
const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) });
const id = (value: string) => encodeURIComponent(value);

export type ProviderKind = "ollama" | "llama_cpp" | "llama_cpp_native" | "openai_compatible" | "mlx_openai_compatible" | "enterprise_gateway";
export type Classification = "PUBLIC" | "INTERNAL" | "CONFIDENTIAL" | "RESTRICTED";
export type Requirement = "STRUCTURED_OUTPUT" | "TEXT_SUMMARY";
export type ReachState = "REACHABLE" | "UNREACHABLE" | "DEGRADED" | "DISABLED" | "NOT_CHECKED";
export type Governance = "AUTHORIZED_AND_MEASURED" | "MEASURED_NOT_AUTHORIZED" | "MEASURED_NOT_APPLICABLE" | "NOT_MEASURED";

export type ProviderSettings = {
  provider_id: string; kind: ProviderKind; display_name: string; enabled: boolean; endpoint: string | null;
  contract_path: string | null; timeout_seconds: number; context_window: number | null;
  capability_requirement: Requirement | null; project_ids: string[]; max_classification: Classification;
  limits: { max_concurrent_calls: number; max_output_tokens: number }; auth_env: string | null;
};
export type ProviderModel = {
  model: string; digest: string | null; loaded: boolean | null; governance: Governance; profile_id: string | null;
  reasons: string[]; authorized_by: string[]; authority: string; explanation: string;
  measured_runtime_version?: string; model_ref?: string;
};
export type ProviderCheck = { check: string; status: string; code: string | null; explanation: string | null; reasons?: string[]; model?: string; profile_id?: string; answer_sha256?: string; answer_chars?: number; authority?: string };
export type Circuit = { state: "CLOSED" | "OPEN" | "HALF_OPEN"; consecutive_failures: number; retry_in_seconds: number | null; last_failure_code: string | null };
export type Attempt = { operation: string; attempt: number; outcome: string; write_outcome: string; at: string; http_status: number | null };
export type Provider = {
  provider_id: string; kind: ProviderKind; display_name: string; source: "setup" | "settings"; enabled: boolean;
  settings: ProviderSettings; auth_env: string | null; auth_env_present: boolean | null;
  reachability: { state: ReachState; code: string | null; explanation: string | null; runtime_version?: string | null };
  models: ProviderModel[]; checks: ProviderCheck[]; checked_at: string | null; circuit: Circuit | null;
  recent_attempts: Attempt[];
  gateway?: { contract: string; code?: string; base_url?: string; auth_env?: string | null; auth_env_present?: boolean | null; contract_max_classification?: Classification; contract_execution_enabled?: boolean; idempotency_honoured?: boolean };
};
export type ProfileRow = { profile_id: string; runtime_family: string; artifact_ref: string; manifest_digest: string; model_ref: string; runtime_version: string; device_profile_id: string };
export type RegistrationRow = { node_id: string; node_name: string; authorized: boolean; max_classification: Classification; project_ids: string[]; allowed_models: string[] };
export type ProvidersOverview = {
  setup_revision: number; providers: Provider[]; governed_use: string;
  catalog: { status: string; profiles: ProfileRow[] }; authorizations: { status: string; registrations: RegistrationRow[] };
};
export type ProviderUpdate = Omit<ProviderSettings, "provider_id" | "enabled">;

export const listProviders = (auth: ControlAuth, f?: Fetcher) =>
  controlRequest<ProvidersOverview>("/control/providers", auth, {}, f);

export const diagnoseProvider = (auth: ControlAuth, providerId: string, probeChat: boolean, f?: Fetcher) =>
  controlRequest<{ provider: Provider }>(`/control/providers/${id(providerId)}/diagnostics`, auth,
    json("POST", { probe_chat: probeChat }), f);

export const toggleProvider = (auth: ControlAuth, providerId: string, enable: boolean, revision: number, f?: Fetcher) =>
  controlRequest<{ provider: Provider; setup_revision: number }>(`/control/providers/${id(providerId)}/${enable ? "enable" : "disable"}`,
    auth, json("POST", { expected_revision: revision }), f);

export const updateProvider = (auth: ControlAuth, providerId: string, update: ProviderUpdate, revision: number, f?: Fetcher) =>
  controlRequest<{ provider: Provider; setup_revision: number }>(`/control/providers/${id(providerId)}`, auth,
    json("PUT", { ...update, expected_revision: revision }), f);
