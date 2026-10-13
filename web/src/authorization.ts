// SPDX-License-Identifier: AGPL-3.0-only
import type { Batch } from "./api";

export type AuthorizationEligibility = {
  enabled: boolean;
  reasons: string[];
};

const SHA256_PATTERN = /^[a-f0-9]{64}$/i;
const DUPLICATE_CONFLICTS = new Set(["DUPLICATE_CONFLICT", "CONFLICTING_DUPLICATE"]);

function isSha256(value: string | undefined): boolean {
  return !!value && SHA256_PATTERN.test(value);
}

function hasRequiredPolicyMetadata(batch: Batch): boolean {
  return batch.parser_policy === "TEXT_FIRST_PYPDF"
    && batch.embedding_policy === "KEYWORD_ONLY"
    && batch.ocr_policy === "DISABLED"
    && batch.cloud_model_policy === "DISABLED"
    && (batch.source_modification_policy === "DISABLED" || batch.source_modification_prohibition === true)
    && !!batch.database_target
    && batch.database_target !== batch.operational_database
    && batch.database_target !== "pados_local_runtime";
}

function backendAllows(batch: Batch, action: string): boolean {
  if (!batch.allowed_actions) return true;
  const actions = new Set(batch.allowed_actions.map((item) => item.toUpperCase()));
  return actions.has(action);
}

export function getAuthorizationEligibility(batch: Batch, mutationPending = false): AuthorizationEligibility {
  const reasons: string[] = [];
  const selectedFiles = batch.selected_files || Object.keys(batch.selected_file_hashes || {});
  const selectedHashCount = Object.keys(batch.selected_file_hashes || {}).length;
  const selectedFileSet = new Set(selectedFiles);
  const selectedMetadata = (batch.files || []).filter((file) => selectedFileSet.has(file.relative_path));

  if (batch.status !== "PENDING_HUMAN_AUTHORIZATION") reasons.push("review is not complete");
  if (!selectedFiles.length || selectedHashCount !== selectedFiles.length) reasons.push("no complete selected-file set");
  if (!selectedMetadata.length || selectedMetadata.length !== selectedFiles.length) reasons.push("selected file metadata is incomplete");
  if (selectedMetadata.some((file) => file.readability !== "READABLE")) reasons.push("selected file is unreadable");
  if (selectedMetadata.some((file) => file.quarantined || file.readability === "QUARANTINED")) reasons.push("selected file is quarantined");
  if (selectedMetadata.some((file) => file.requires_ocr || file.ocr_status === "OCR_REQUIRED")) reasons.push("OCR is required while OCR is disabled");
  if (selectedMetadata.some((file) => DUPLICATE_CONFLICTS.has(file.duplicate_status))) reasons.push("selected file has a conflicting duplicate condition");
  if (!isSha256(batch.manifest_sha256)) reasons.push("manifest identity is missing or malformed");
  if (!batch.project_id) reasons.push("project identity is missing");
  if (!hasRequiredPolicyMetadata(batch)) reasons.push("authorization policy metadata is incomplete or out of scope");
  if (batch.source_modification_prohibition === false) reasons.push("source modification is not prohibited");
  if (!backendAllows(batch, "AUTHORIZE")) reasons.push("backend does not allow authorization");
  if (!backendAllows(batch, "AUTHORIZE_AND_INGEST")) reasons.push("backend does not allow authorize-and-ingest");
  if (mutationPending) reasons.push("authorization mutation is in progress");

  return { enabled: reasons.length === 0, reasons };
}
