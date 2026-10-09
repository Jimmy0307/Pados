// SPDX-License-Identifier: AGPL-3.0-only
/** Remote Operations client. Sends intents, reads projections. Holds no context. */

export type Bucket = "WAITING_APPROVAL" | "FAILED" | "RUNNING" | "COMPLETED";

export interface RemoteRun {
  run_id: string;
  project_id: string;
  goal?: string | null;
  status?: string | null;
  progress_status?: string | null;
  semantic_commit_status?: string | null;
  human_action_required?: boolean | null;
  started_at?: string | null;
  finished_at?: string | null;
  approval_ref?: string | null;
  artifacts?: { ref?: string; digest?: string; path?: string }[];
  bucket: Bucket;
}

export interface AttentionProjection {
  needs_attention: RemoteRun[];
  running: RemoteRun[];
  waiting_approval: RemoteRun[];
  failed: RemoteRun[];
  completed: RemoteRun[];
  projection: string;
}

export interface RemoteGoalProjection {
  goal_id: string;
  title: string;
  objective: string;
  state: string;
  required_capabilities: string[];
  dependencies: string[];
}

export interface RemoteTaskProjection {
  request_id: string;
  task_id: string;
  project_id: string;
  original_intent: string;
  classification: string;
  priority: string;
  deadline: string | null;
  scope_version_ref: string;
  decomposition_status: string;
  work_request_status: string;
  created_at: string;
  goals: RemoteGoalProjection[];
  assignments: { worker_id: string; capability: string; reason?: string }[];
  artifacts: { artifact_id: string; digest?: string }[];
  evidence: { evidence_id: string; summary?: string }[];
  receipts: { receipt_id: string }[];
}

export interface WorkMapProjection {
  projects: string[];
  tasks: RemoteTaskProjection[];
  runs: RemoteRun[];
  projection: string;
  semantic_authority: string;
}

export interface RemoteNode {
  node_id: string;
  node_name?: string | null;
  state: string;
  stale: boolean;
  authorized: boolean;
}

export interface RemoteNodes {
  status: string;
  nodes: RemoteNode[];
  model_endpoints_exposed: false;
}

export interface IntentOutcome {
  intent_id: string;
  outcome: string;
  run_id?: string;
  ledger_entry_hash: string;
}

export interface RemoteSignIn {
  actor: string;
  proof: string;
}

/** Server-derived binding the human is approving (shown next to the authenticator prompt). */
export interface StepUpBinding {
  action: "APPROVE" | "REJECT";
  project_id: string;
  task_id: string;
  goal_id: string | null;
  candidate_id: string;
  candidate_digest: string;
  classification: string;
  expected_scope_version: number;
  kind: string;
}

export interface StepUpChallenge {
  challenge_id: string;
  expires_at: string;
  binding_digest: string;
  binding: StepUpBinding;
  public_key: { challenge: string; rpId: string; timeout: number; userVerification: string;
    allowCredentials: { type: "public-key"; id: string }[] };
}

/** WebAuthn assertion fields (base64url), produced by the platform authenticator. */
export interface PresenceAssertion {
  credential_id: string;
  client_data_json: string;
  authenticator_data: string;
  signature: string;
  user_handle?: string;
}

export type PresenceAuthenticator = (options: StepUpChallenge["public_key"]) => Promise<PresenceAssertion>;
export type ConfirmBinding = (binding: StepUpBinding) => boolean | Promise<boolean>;

export function base64UrlToBytes(value: string): Uint8Array<ArrayBuffer> {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((value.length + 3) % 4);
  const raw = atob(padded);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let index = 0; index < raw.length; index += 1) out[index] = raw.charCodeAt(index);
  return out;
}

export function bytesToBase64Url(value: ArrayBuffer | Uint8Array): string {
  const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
  let raw = "";
  for (const byte of bytes) raw += String.fromCharCode(byte);
  return btoa(raw).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Browser WebAuthn: user verification is required; the private key never leaves the authenticator. */
export const browserAuthenticator: PresenceAuthenticator = async (options) => {
  if (typeof navigator === "undefined" || navigator.credentials === undefined || typeof PublicKeyCredential === "undefined") {
    throw new RemoteApiError(412, "WEBAUTHN_UNAVAILABLE");
  }
  const signed = await navigator.credentials.get({
    publicKey: {
      challenge: base64UrlToBytes(options.challenge),
      rpId: options.rpId,
      timeout: options.timeout,
      userVerification: "required",
      allowCredentials: options.allowCredentials.map((item) => ({ type: "public-key", id: base64UrlToBytes(item.id) })),
    },
  }) as PublicKeyCredential | null;
  if (!signed) throw new RemoteApiError(401, "WEBAUTHN_CANCELLED");
  const response = signed.response as AuthenticatorAssertionResponse;
  const assertion: PresenceAssertion = {
    credential_id: bytesToBase64Url(signed.rawId),
    client_data_json: bytesToBase64Url(response.clientDataJSON),
    authenticator_data: bytesToBase64Url(response.authenticatorData),
    signature: bytesToBase64Url(response.signature),
  };
  if (response.userHandle) assertion.user_handle = bytesToBase64Url(response.userHandle);
  return assertion;
};

export class RemoteApiError extends Error {
  constructor(readonly status: number, readonly detail: string) {
    super(`${status} ${detail}`);
  }
}

const PREFIX = "/remote/v1";

function newIntentId(): string {
  const bytes = new Uint8Array(12);
  globalThis.crypto.getRandomValues(bytes);
  return `ri-${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
}

export class RemoteClient {
  // Wrapped so the browser fetch is never invoked with a foreign `this`.
  private readonly baseUrl: string;
  // Server-issued session handle: binding context for step-up, kept in memory only.
  private sessionHandle: string | null = null;

  constructor(
    private readonly signIn: RemoteSignIn,
    private readonly fetcher: typeof fetch = (input, init) => fetch(input, init),
    remoteEndpoint = "",
  ) {
    const value = remoteEndpoint.trim();
    if (!value) {
      this.baseUrl = "";
    } else {
      const parsed = new URL(value);
      const localHttp = parsed.protocol === "http:" && ["localhost", "127.0.0.1", "::1"].includes(parsed.hostname);
      if ((parsed.protocol !== "https:" && !localHttp) || parsed.username || parsed.href.includes("@") ||
          parsed.pathname !== "/" || parsed.search || parsed.hash) {
        throw new Error("REMOTE_ENDPOINT_INVALID");
      }
      this.baseUrl = parsed.origin;
    }
  }

  private async call<T>(path: string, init: RequestInit = {}): Promise<T> {
    const response = await this.fetcher(`${this.baseUrl}${PREFIX}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        "X-PADOS-Remote-Actor": this.signIn.actor,
        "X-PADOS-Remote-Proof": this.signIn.proof,
        ...(init.headers || {}),
      },
    });
    if (!response.ok) {
      let detail = response.statusText;
      try {
        const body = (await response.json()) as { detail?: unknown };
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } catch {
        /* keep status text */
      }
      throw new RemoteApiError(response.status, detail);
    }
    return (await response.json()) as T;
  }

  attention(): Promise<AttentionProjection> {
    return this.call("/attention");
  }

  workMap(): Promise<WorkMapProjection> {
    return this.call("/work-map");
  }

  connectivity(): Promise<ConnectivityProjection> {
    return this.call("/connectivity");
  }

  nodes(): Promise<RemoteNodes> {
    return this.call("/nodes");
  }

  /** START_TASK carries a short human message only; PADOS rebuilds context server-side. */
  startTask(
    projectId: string,
    message: string,
    priority: "LOW" | "NORMAL" | "HIGH" = "NORMAL",
    deadline?: string,
  ): Promise<IntentOutcome> {
    const body: Record<string, unknown> = {
      intent_id: newIntentId(), project_id: projectId, intent_type: "START_TASK", human_message: message,
    };
    if (priority !== "NORMAL") body.priority = priority;
    if (deadline) body.deadline = deadline;
    return this.call("/intents", {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  private async session(): Promise<Record<string, string>> {
    if (!this.sessionHandle) {
      const opened = await this.call<{ session_handle: string }>("/sessions", { method: "POST" });
      this.sessionHandle = opened.session_handle;
    }
    return { "X-PADOS-Remote-Session": this.sessionHandle };
  }

  /**
   * A consequential decision needs a fresh, user-verified step-up for exactly this candidate:
   * server challenge -> authenticator (biometric / PIN) -> single-use proof -> decision intent.
   * The client chooses no binding field; the proof is used once and never stored.
   */
  async decide(
    run: RemoteRun,
    decision: "approve" | "reject",
    authenticator: PresenceAuthenticator = browserAuthenticator,
    confirm: ConfirmBinding = () => true,
  ): Promise<IntentOutcome> {
    if (!run.approval_ref) throw new RemoteApiError(409, "APPROVAL_REF_MISSING");
    const ref = encodeURIComponent(run.approval_ref);
    const headers = await this.session();
    const challenge = await this.call<StepUpChallenge>(`/approvals/${ref}/step-up`, {
      method: "POST", headers, body: JSON.stringify({ action: decision.toUpperCase() }),
    });
    if (challenge.binding.candidate_id !== run.run_id && challenge.binding.task_id !== run.run_id) {
      throw new RemoteApiError(409, "STEP_UP_BINDING_MISMATCH");
    }
    if (!(await confirm(challenge.binding))) throw new RemoteApiError(409, "STEP_UP_DECLINED");
    const assertion = await authenticator(challenge.public_key);
    const verified = await this.call<{ proof_ref: string }>(
      `/approvals/${ref}/step-up/${encodeURIComponent(challenge.challenge_id)}/verify`,
      { method: "POST", headers, body: JSON.stringify({ assertion }) },
    );
    return this.call(`/approvals/${ref}/${decision}`, {
      method: "POST",
      headers,
      body: JSON.stringify({
        intent_id: newIntentId(), project_id: run.project_id, target_run_id: run.run_id,
        step_up_proof: verified.proof_ref,
      }),
    });
  }

  /** Register this device's authenticator: returns a public descriptor for the operator to enrol. */
  async presenceDescriptor(label: string): Promise<Record<string, unknown>> {
    const options = await this.call<{ public_key: { challenge: string; rp: { id: string; name: string };
      user: { id: string; name: string; displayName: string }; pubKeyCredParams: { type: "public-key"; alg: number }[];
      timeout: number } }>("/presence/enrollment-options", { method: "POST" });
    const key = options.public_key;
    const created = await navigator.credentials.create({ publicKey: {
      challenge: base64UrlToBytes(key.challenge), rp: key.rp,
      user: { ...key.user, id: base64UrlToBytes(key.user.id) }, pubKeyCredParams: key.pubKeyCredParams,
      authenticatorSelection: { userVerification: "required", residentKey: "preferred" }, attestation: "none",
      timeout: key.timeout,
    } }) as PublicKeyCredential | null;
    if (!created) throw new RemoteApiError(401, "WEBAUTHN_CANCELLED");
    const response = created.response as AuthenticatorAttestationResponse;
    return this.call("/presence/enrollment-descriptor", { method: "POST", body: JSON.stringify({
      challenge: key.challenge, label,
      registration: { client_data_json: bytesToBase64Url(response.clientDataJSON),
        attestation_object: bytesToBase64Url(response.attestationObject) } }) });
  }

  control(run: RemoteRun, action: "pause" | "resume" | "cancel"): Promise<IntentOutcome> {
    return this.call(`/jobs/${encodeURIComponent(run.run_id)}/${action}`, {
      method: "POST",
      body: JSON.stringify({ intent_id: newIntentId(), project_id: run.project_id }),
    });
  }
}

export function projectsOf(view: AttentionProjection): { project_id: string; counts: Record<Bucket, number> }[] {
  const rows = [...view.running, ...view.waiting_approval, ...view.failed, ...view.completed];
  const map = new Map<string, Record<Bucket, number>>();
  for (const row of rows) {
    const counts = map.get(row.project_id) || { WAITING_APPROVAL: 0, FAILED: 0, RUNNING: 0, COMPLETED: 0 };
    counts[row.bucket] += 1;
    map.set(row.project_id, counts);
  }
  return [...map.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([project_id, counts]) => ({ project_id, counts }));
}

export interface LinkStatus {
  link: string;
  state: "CONNECTED" | "INTERMITTENT" | "OFFLINE" | "RECOVERING" | "UNKNOWN";
  last_connected?: string | null;
  last_disconnected?: string | null;
  recovered_at?: string | null;
}

export interface ConnectivityProjection {
  status?: string;
  home_base: string;
  links?: Record<string, LinkStatus>;
}

export type BannerKind = "online" | "wan-offline" | "reconnecting" | "disconnected";

export interface Banner {
  kind: BannerKind;
  text: string;
}

/**
 * Connectivity banner. Transport loss is never presented as Home Base failure:
 * - this browser offline            -> DISCONNECTED
 * - Home Base unreachable from here -> RECONNECTING (Home Base keeps running locally)
 * - reachable, WAN down at the base -> HOME BASE RUNNING / WAN OFFLINE
 */
export function deriveBanner(input: { browserOnline: boolean; reachable: boolean; connectivity?: ConnectivityProjection | null }): Banner {
  if (!input.browserOnline) return { kind: "disconnected", text: "DISCONNECTED — this device has no network" };
  if (!input.reachable) return { kind: "reconnecting", text: "RECONNECTING — Home Base unreachable from this device" };
  const wan = input.connectivity?.links?.WAN?.state;
  if (wan === "OFFLINE" || wan === "INTERMITTENT") return { kind: "wan-offline", text: `HOME BASE RUNNING / WAN ${wan}` };
  return { kind: "online", text: "HOME BASE ONLINE / REMOTE CONNECTED" };
}
