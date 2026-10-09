// SPDX-License-Identifier: AGPL-3.0-only
export type Project = {
  id: string;
  name?: string;
  slug?: string;
  description?: string | null;
  corpus_status?: CorpusStatus;
  root_path?: string;
  status?: string;
  updated_at?: string;
  memory_count?: number;
  task_count?: number;
  run_count?: number;
};

export type ApiStatus = "OK" | "WARNING" | "ERROR" | "UNKNOWN";

export type Pagination = {
  page: number;
  page_size: number;
  total: number;
  has_next: boolean;
};

export type ControlHealth = {
  status: ApiStatus;
  version: string;
  web_version: string;
  environment: string;
  database: {
    status: ApiStatus;
    name: string;
    host: string;
    port: number;
    version: string | null;
    pgvector: boolean | null;
  };
  migration: {
    status: ApiStatus;
    current_revision: string | string[] | null;
    expected_head: string | string[] | null;
  };
  metrics: Record<string, number>;
  checks: Array<{ name: string; status: ApiStatus; detail?: string }>;
};

export type ControlMemory = {
  memory_id: string;
  project_id: string;
  memory_type: string;
  title: string;
  content_summary: string;
  source_type: string;
  source_uri: string;
  source_hash?: string | null;
  source_trust_level: string;
  verification_status: string;
  freshness_status: string;
  importance: number;
  retrieval_priority: number;
  estimated_tokens: number;
  score: number;
  retrieval_reason: string;
};

export type ControlDatabaseTable = {
  schema: string;
  name: string;
  kind: string;
  estimated_row_count: number | null;
};

export type ControlDatabaseOverview = {
  status: string;
  database: string;
  schema: string;
  postgresql_version: string;
  pgvector: boolean;
  migration: ControlHealth["migration"];
  metrics: Record<string, number>;
  tables: ControlDatabaseTable[];
  schemas: Array<{ name: string; kind: string }>;
  data_map: Array<{ entity: string; tables: string[] }>;
};

export type ControlTableRows = {
  status: string;
  table: {
    schema: string;
    name: string;
    columns: Array<{ name: string; type: string; nullable: boolean; primary_key: boolean; foreign_key: boolean; indexed: boolean }>;
  };
  rows: Array<Record<string, unknown>>;
  pagination: Pagination;
  exact_count: boolean;
};

export type ControlActivity = {
  id: string;
  project_id?: string;
  status: string;
  name: string;
  timestamp?: string;
  source: string;
  payload: Record<string, unknown>;
};

export type MissionControlProject = {
  project_id: string;
  project: Project;
  project_status: string;
  current_goal: { id: string; objective: string; status: string } | null;
  blockers: Array<{ id: string; description: string; severity: string; status: string }>;
  next_decisions: Array<{ id: string; question: string; status: string; source?: string }>;
  active_runs: Array<{ id: string; name?: string; status: string; validation_status?: string }>;
  recent_accepted_results: Array<{ id: string; title?: string; accepted_scope_version: number }>;
};

export type EnterpriseCapability = {
  capability_id: string;
  capability_type: string;
  version: string;
  execution_mode: string;
  max_classification: string;
  health: string;
  state: string;
  reason?: string | null;
};

export type EnterpriseEvidence = {
  run_id: string;
  project_id: string;
  provider_id: string;
  execution_class: string;
  result_status?: string | null;
  package_hash: string;
  source_ids: string[];
  classifications: string[];
  backends: string[];
  reranker: { reranker_id?: string; model?: string; mode?: string; applied?: boolean };
  authorization: { decision: string; ceiling: string; withheld_count: number };
};

/** Optional, read-only Enterprise Capability Pack projection. Evidence is not truth. */
export type EnterpriseVisibility = {
  status: string;
  core: string;
  capabilities: EnterpriseCapability[];
  recent_evidence: EnterpriseEvidence[];
  authorization_failures: Array<{ kind: string; run_id?: string; capability_id?: string; count?: number; reason?: string | null }>;
  projection_only: true;
  semantic_authority: "NONE";
};

export type ComputeNodeView = {
  node_id: string;
  node_name?: string | null;
  state: string;
  stale: boolean;
  authorized: boolean;
  platform?: string | null;
  architecture?: string | null;
  capabilities: string[];
  models: Array<{ model_id: string; availability: string }>;
  current_load?: { running: number; queued: number; max_concurrent_jobs: number; max_queue_depth: number } | null;
  last_successful_check?: string | null;
  last_execution?: { run_id: string; model_id: string; status: string; finished_at: string } | null;
  execution_class: string;
  reason?: string | null;
};

/** Optional, read-only compute-node projection (probe evidence and receipts only). */
export type ComputeNodesVisibility = {
  status: string;
  nodes: ComputeNodeView[];
  projection_only: true;
};

export type MissionControl = {
  projects: MissionControlProject[];
  enterprise?: EnterpriseVisibility;
  compute_nodes?: ComputeNodesVisibility;
  system_health: {
    alive: boolean;
    configured: boolean;
    usable: boolean;
    ready: boolean;
    degraded: boolean;
    blocked: boolean;
    state: string;
    checks: Array<{ name: string; status: string; detail: string }>;
  };
  projection_only: true;
  authority: string;
};

export type DecisionInboxItem = {
  id: string;
  project_id: string;
  project: string;
  item: string;
  type: string;
  priority: number;
  status: string;
  reason_source: string;
  required_human_action: string;
  target: string;
  projection_only: true;
};

export type DecisionInbox = {
  items: DecisionInboxItem[];
  count: number;
  projection_only: true;
  authority: string;
};

export type CorpusCounts = Record<string, number>;

export type CorpusStatus = {
  status: string;
  database?: string;
  revision?: string;
  operational_database?: string;
  operational_read_only?: boolean;
  embedding_policy?: string;
  project_projection?: boolean;
  counts: CorpusCounts;
  pending_review_batches?: number;
};

export type BatchFile = {
  filename: string;
  relative_path: string;
  file_size: number;
  sha256: string;
  mime_type: string;
  readability: string;
  duplicate_status: string;
  duplicate_group?: string | null;
  quarantined?: boolean;
  requires_ocr?: boolean;
  ocr_status: string;
};

export type Batch = {
  batch_id: string;
  project_id: string;
  status: string;
  files?: BatchFile[];
  selected_files?: string[];
  selected_file_hashes?: Record<string, string>;
  approved_root?: string;
  database_target?: string;
  operational_database?: string;
  parser_policy?: string;
  embedding_policy?: string;
  ocr_policy?: string;
  cloud_model_policy?: string;
  source_modification_policy?: string;
  source_modification_prohibition?: boolean;
  document_ceiling?: number;
  allowed_actions?: string[];
  manifest_sha256?: string;
  database_record?: Record<string, unknown>;
  idempotency?: string;
};

export type BatchResponse = {
  status?: string;
  batch: Batch;
  authorization_present?: boolean;
  manifest_sha256?: string;
  allowed_actions?: string[];
};

export type Document = {
  id: string;
  canonical_title?: string;
  status?: string;
  latest_version?: Record<string, unknown> | null;
  pages?: number;
  chunks?: number;
};

export type SearchResult = {
  source_filename: string;
  source_path_relative: string;
  page_number: number;
  matched_excerpt: string;
  deterministic_score: number;
  match_count: number;
  match_explanation: Record<string, unknown>;
  document_id: string;
  document_version_id: string;
  chunk_id: string;
  evidence_span_id: string;
  document_title: string;
  search_mode: string;
};

export type ApiError = Error & { code?: string; write_occurred?: boolean };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { Accept: "application/json", ...(init?.headers || {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = body?.detail;
    const error = new Error(detail?.message || `API request failed (${response.status})`) as ApiError;
    error.code = detail?.code;
    error.write_occurred = detail?.write_occurred;
    throw error;
  }
  return body as T;
}

export async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  const payload = (await response.json().catch(() => ({}))) as { detail?: string };
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status})`);
  return payload as T;
}

export function queryString(values: Record<string, string | number | undefined | null>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const result = params.toString();
  return result ? `?${result}` : "";
}

export function formatDate(value?: string): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

export function shortId(value: string): string {
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

export function listProjects(): Promise<{ projects: Project[] }> {
  return request("/api/projects");
}

export function getCorpusStatus(projectId: string): Promise<{ corpus: CorpusStatus }> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/corpus/status`);
}

export function listBatches(projectId: string): Promise<{ batches: Batch[] }> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/corpus/batches`);
}

export function listDocuments(projectId: string): Promise<{ documents: Document[] }> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/corpus/documents`);
}

function normalizeBatchResponse(response: BatchResponse): BatchResponse {
  return {
    ...response,
    batch: {
      ...response.batch,
      ...(response.manifest_sha256 ? { manifest_sha256: response.manifest_sha256 } : {}),
      ...(response.allowed_actions ? { allowed_actions: response.allowed_actions } : {}),
    },
  };
}

export function getBatch(batchId: string): Promise<BatchResponse> {
  return request<BatchResponse>(`/api/corpus/batches/${encodeURIComponent(batchId)}`).then(normalizeBatchResponse);
}

export function createBatch(projectId: string, files: File[]): Promise<BatchResponse> {
  const body = new FormData();
  files.forEach((file) => body.append("files", file, file.name));
  return request<BatchResponse>(`/api/projects/${encodeURIComponent(projectId)}/corpus/batches`, { method: "POST", body }).then(normalizeBatchResponse);
}

export function scanBatch(batchId: string): Promise<BatchResponse> {
  return request<BatchResponse>(`/api/corpus/batches/${encodeURIComponent(batchId)}/scan`, { method: "POST" }).then(normalizeBatchResponse);
}

export function reviewBatch(batchId: string, selectedFiles: string[]): Promise<BatchResponse> {
  return request<BatchResponse>(`/api/corpus/batches/${encodeURIComponent(batchId)}/review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ selected_files: selectedFiles }),
  }).then(normalizeBatchResponse);
}

export function authorizeBatch(batch: Batch): Promise<Record<string, unknown>> {
  return request(`/api/corpus/batches/${encodeURIComponent(batch.batch_id)}/authorize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      manifest_sha256: batch.manifest_sha256,
      selected_file_hashes: batch.selected_file_hashes,
    }),
  });
}

export function ingestBatch(batchId: string): Promise<Record<string, unknown>> {
  return request(`/api/corpus/batches/${encodeURIComponent(batchId)}/ingest`, { method: "POST" });
}

export function retryBatch(batchId: string): Promise<Record<string, unknown>> {
  return request(`/api/corpus/batches/${encodeURIComponent(batchId)}/retry`, { method: "POST" });
}

export function searchCorpus(projectId: string, query: string): Promise<{ results: SearchResult[]; result_count: number }> {
  return request(`/api/projects/${encodeURIComponent(projectId)}/corpus/search`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, limit: 25 }),
  });
}

export function getEvidence(evidenceId: string): Promise<{ evidence: Record<string, unknown> }> {
  return request(`/api/corpus/evidence/${encodeURIComponent(evidenceId)}`);
}
