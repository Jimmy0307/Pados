// SPDX-License-Identifier: AGPL-3.0-only
import { describe, expect, it } from "vitest";
import { getAuthorizationEligibility } from "./authorization";
import type { Batch } from "./api";

const validBatch = (overrides: Partial<Batch> = {}): Batch => ({
  batch_id: "batch",
  project_id: "project",
  status: "PENDING_HUMAN_AUTHORIZATION",
  files: [{
    filename: "paper.pdf",
    relative_path: "paper.pdf",
    file_size: 3,
    sha256: "b".repeat(64),
    mime_type: "application/pdf",
    readability: "READABLE",
    duplicate_status: "NEW_CONTENT",
    duplicate_group: null,
    ocr_status: "DETERMINED_AT_INGEST;OCR_DISABLED",
  }],
  selected_files: ["paper.pdf"],
  selected_file_hashes: { "paper.pdf": "b".repeat(64) },
  manifest_sha256: "a".repeat(64),
  database_target: "pados_corpus_operator_acceptance_20260803",
  parser_policy: "TEXT_FIRST_PYPDF",
  embedding_policy: "KEYWORD_ONLY",
  ocr_policy: "DISABLED",
  cloud_model_policy: "DISABLED",
  source_modification_policy: "DISABLED",
  ...overrides,
});

describe("authorization eligibility", () => {
  it("enables both authorization actions for valid reviewed metadata", () => {
    expect(getAuthorizationEligibility(validBatch())).toEqual({ enabled: true, reasons: [] });
  });

  it("does not treat a null duplicate group as a conflict", () => {
    expect(getAuthorizationEligibility(validBatch({ files: [{ ...validBatch().files![0], duplicate_group: null }] }))).toEqual({ enabled: true, reasons: [] });
  });

  it("keeps the locked OCR, retrieval, and source policies authorizable", () => {
    expect(getAuthorizationEligibility(validBatch({
      ocr_policy: "DISABLED",
      embedding_policy: "KEYWORD_ONLY",
      source_modification_policy: undefined,
      source_modification_prohibition: true,
    }))).toEqual({ enabled: true, reasons: [] });
  });

  it("accepts a complete four-file reviewed selection", () => {
    const template = validBatch().files![0];
    const files = Array.from({ length: 4 }, (_, index) => ({ ...template, filename: `paper-${index}.pdf`, relative_path: `paper-${index}.pdf`, sha256: String(index).repeat(64) }));
    const batch = validBatch({
      files,
      selected_files: files.map((file) => file.relative_path),
      selected_file_hashes: Object.fromEntries(files.map((file) => [file.relative_path, file.sha256])),
    });
    expect(getAuthorizationEligibility(batch)).toEqual({ enabled: true, reasons: [] });
  });

  it("reports missing manifest identity", () => {
    const result = getAuthorizationEligibility(validBatch({ manifest_sha256: undefined }));
    expect(result.enabled).toBe(false);
    expect(result.reasons).toContain("manifest identity is missing or malformed");
  });

  it("reports unreadable and OCR-required files", () => {
    const result = getAuthorizationEligibility(validBatch({ files: [{ ...validBatch().files![0], readability: "UNREADABLE", requires_ocr: true }] }));
    expect(result.reasons).toEqual(expect.arrayContaining(["selected file is unreadable", "OCR is required while OCR is disabled"]));
  });

  it("reports a pending authorization mutation", () => {
    const result = getAuthorizationEligibility(validBatch(), true);
    expect(result.reasons).toContain("authorization mutation is in progress");
  });
});
