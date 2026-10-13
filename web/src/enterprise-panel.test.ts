// SPDX-License-Identifier: AGPL-3.0-only
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { EnterpriseVisibility } from "./api";
import { EnterprisePanel } from "./enterprise-panel";

const enterprise: EnterpriseVisibility = {
  status: "ENTERPRISE_EXTENSION_READY",
  core: "CORE_READY",
  capabilities: [{ capability_id: "knowledge.local", capability_type: "KNOWLEDGE_RETRIEVAL", version: "1",
    execution_mode: "IN_PROCESS", max_classification: "RESTRICTED", health: "READY", state: "ENTERPRISE_CAPABILITY_READY" }],
  recent_evidence: [{ run_id: "12345678-1234-1234-1234-123456789abc", project_id: "p", provider_id: "PADOS_FILE_DIRECT_MAKER",
    execution_class: "LOCAL", package_hash: "a".repeat(64), source_ids: ["sha256:1", "sha256:2"],
    classifications: ["INTERNAL", "PUBLIC"], backends: [], reranker: {},
    authorization: { decision: "ALLOW", ceiling: "CONFIDENTIAL", withheld_count: 1 } }],
  authorization_failures: [{ kind: "EVIDENCE_WITHHELD", run_id: "12345678-1234-1234-1234-123456789abc", count: 1 }],
  projection_only: true,
  semantic_authority: "NONE",
};

describe("Enterprise panel", () => {
  it("renders nothing when the extension block is absent", () => {
    expect(renderToStaticMarkup(createElement(EnterprisePanel, {}))).toBe("");
  });

  it("shows capability health, provenance, provider, execution class and authorization failures", () => {
    const html = renderToStaticMarkup(createElement(EnterprisePanel, { enterprise }));
    expect(html).toContain("Enterprise capabilities");
    expect(html).toContain("knowledge.local");
    expect(html).toContain("PADOS_FILE_DIRECT_MAKER · LOCAL · 2 sources (INTERNAL, PUBLIC)");
    expect(html).toContain("EVIDENCE_WITHHELD");
    expect(html).toContain("CORE READY");
  });
});
