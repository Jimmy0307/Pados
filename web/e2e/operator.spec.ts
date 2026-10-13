import { test, expect, type Page } from "@playwright/test";

const projectId = "325f8168-efec-4818-99ac-b00a03d6accd";

function reviewedBatch(batchId: string, status = "PENDING_HUMAN_AUTHORIZATION") {
  const fileSha = "b".repeat(64);
  return {
    batch_id: batchId,
    project_id: projectId,
    status,
    files: [{ filename: "paper.pdf", relative_path: "paper.pdf", sha256: fileSha, file_size: 3, mime_type: "application/pdf", readability: "READABLE", duplicate_status: "NEW_CONTENT", duplicate_group: null, ocr_status: "DETERMINED_AT_INGEST;OCR_DISABLED" }],
    selected_files: ["paper.pdf"],
    selected_file_hashes: { "paper.pdf": fileSha },
    database_target: "pados_corpus_operator_acceptance_20260803",
    parser_policy: "TEXT_FIRST_PYPDF",
    embedding_policy: "KEYWORD_ONLY",
    ocr_policy: "DISABLED",
    cloud_model_policy: "DISABLED",
    source_modification_policy: "DISABLED",
  };
}

async function routeReviewedBatch(page: Page, batchId: string, status: { value: string }) {
  const batch = reviewedBatch(batchId);
  await page.route(`**/api/projects/${projectId}/corpus/batches`, (route) => route.fulfill({ json: { batches: [{ ...batch, status: status.value }] } }));
  await page.route(`**/api/corpus/batches/${batchId}`, (route) => route.fulfill({ json: { status: "ok", batch: { ...batch, status: status.value }, manifest_sha256: "a".repeat(64), authorization_present: status.value === "AUTHORIZED" || status.value === "PUBLISHED" } }));
  return batch;
}

test.beforeEach(async ({ page }) => {
  await page.route("**/api/projects", (route) => route.fulfill({ json: { projects: [{ id: projectId, name: "PADOS Demo Project" }] } }));
  await page.route(`**/api/projects/${projectId}/corpus/status`, (route) => route.fulfill({ json: { corpus: { status: "READY", database: "pados_corpus_runtime", revision: "20260730_0011", embedding_policy: "KEYWORD_ONLY", counts: { documents: 5, pages: 63, chunks: 203, evidence_spans: 203 }, pending_review_batches: 0 } } }));
  await page.route(`**/api/projects/${projectId}/corpus/batches`, (route) => route.fulfill({ json: { batches: [] } }));
  await page.route(`**/api/projects/${projectId}/corpus/documents`, (route) => route.fulfill({ json: { documents: [] } }));
});

test("operator workspace loads with policy and project-scoped metrics", async ({ page }) => {
  await page.goto("/operator.html");
  await expect(page.getByRole("heading", { name: "Corpus workspace" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "PADOS Demo Project" })).toBeVisible();
  await expect(page.getByText("TEXT_FIRST_PYPDF")).toBeVisible();
  await expect(page.getByText("OCR / cloud models")).toBeVisible();
  await expect(page.getByText("203").first()).toBeVisible();
});

test("staging is explicit and does not authorize or ingest automatically", async ({ page }) => {
  await page.route(`**/api/projects/${projectId}/corpus/batches`, async (route) => {
    if (route.request().method() === "POST") {
      await route.fulfill({ status: 201, json: { batch: { batch_id: "11111111-1111-1111-1111-111111111111", project_id: projectId, status: "STAGED", files: [{ filename: "paper.pdf", relative_path: "paper.pdf", sha256: "a".repeat(64), file_size: 3, mime_type: "application/pdf", readability: "STAGED", duplicate_status: "UNKNOWN", ocr_status: "DISABLED_POLICY" }] }, manifest_sha256: "b".repeat(64) } });
    } else {
      await route.fulfill({ json: { batches: [] } });
    }
  });
  await page.goto("/operator.html");
  await page.locator("input[type=file]").setInputFiles({ name: "paper.pdf", mimeType: "application/pdf", buffer: Buffer.from("pdf") });
  await page.getByRole("button", { name: "Stage selected files" }).click();
  await expect(page.getByText("STAGED").first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Authorize", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Authorize and Ingest" })).toBeDisabled();
});

test("reviewed metadata enables authorization after detail refresh", async ({ page }) => {
  const batchId = "22222222-2222-4222-8222-222222222222";
  await routeReviewedBatch(page, batchId, { value: "PENDING_HUMAN_AUTHORIZATION" });

  await page.goto("/operator.html");
  await page.getByRole("button", { name: "Inspect", exact: true }).click();

  await expect(page.getByRole("button", { name: "Authorize", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Authorize and Ingest", exact: true })).toBeEnabled();
  await expect(page.getByRole("list", { name: "Authorization disabled reasons" })).toHaveCount(0);
});

test("successful Authorize updates the batch without ingest", async ({ page }) => {
  const batchId = "33333333-3333-4333-8333-333333333333";
  const status = { value: "PENDING_HUMAN_AUTHORIZATION" };
  await routeReviewedBatch(page, batchId, status);
  let authorizeCount = 0;
  let ingestCount = 0;
  await page.route(`**/api/corpus/batches/${batchId}/authorize`, async (route) => {
    authorizeCount += 1;
    status.value = "AUTHORIZED";
    await route.fulfill({ json: { status: "ok", batch: reviewedBatch(batchId, "AUTHORIZED"), authorization: { status: "AUTHORIZED" } } });
  });
  await page.route(`**/api/corpus/batches/${batchId}/ingest`, async (route) => {
    ingestCount += 1;
    await route.fulfill({ json: { status: "ok" } });
  });

  await page.goto("/operator.html");
  await page.getByRole("button", { name: "Inspect", exact: true }).click();
  await page.getByRole("button", { name: "Authorize", exact: true }).click();

  await expect(page.getByText("AUTHORIZED", { exact: true }).last()).toBeVisible();
  expect(authorizeCount).toBe(1);
  expect(ingestCount).toBe(0);
});

test("Authorize and Ingest performs one authorization and one ingest", async ({ page }) => {
  const batchId = "44444444-4444-4444-8444-444444444444";
  const status = { value: "PENDING_HUMAN_AUTHORIZATION" };
  await routeReviewedBatch(page, batchId, status);
  let authorizeCount = 0;
  let ingestEndpointCount = 0;
  let authorizationExpiry: unknown = "present";
  await page.route(`**/api/corpus/batches/${batchId}/authorize`, async (route) => {
    authorizeCount += 1;
    authorizationExpiry = route.request().postDataJSON()?.expires_hours;
    status.value = "AUTHORIZED";
    await route.fulfill({ json: { status: "ok", batch: reviewedBatch(batchId, "AUTHORIZED"), authorization: { status: "AUTHORIZED" } } });
  });
  await page.route(`**/api/corpus/batches/${batchId}/ingest`, async (route) => {
    ingestEndpointCount += 1;
    status.value = "PUBLISHED";
    await route.fulfill({ json: { status: "ok", batch_id: batchId, state: "PUBLISHED", ingest: { status: "ok" } } });
  });

  await page.goto("/operator.html");
  await page.getByRole("button", { name: "Inspect", exact: true }).click();
  await page.getByRole("button", { name: "Authorize and Ingest", exact: true }).click();

  await expect(page.getByText("PUBLISHED", { exact: true }).last()).toBeVisible();
  expect(authorizeCount).toBe(1);
  expect(authorizationExpiry).toBeUndefined();
  expect(ingestEndpointCount).toBe(1);
});

test("authorization failure prevents ingest", async ({ page }) => {
  const batchId = "55555555-5555-4555-8555-555555555555";
  const status = { value: "PENDING_HUMAN_AUTHORIZATION" };
  await routeReviewedBatch(page, batchId, status);
  let authorizeCount = 0;
  let ingestCount = 0;
  await page.route(`**/api/corpus/batches/${batchId}/authorize`, async (route) => {
    authorizeCount += 1;
    await route.fulfill({ status: 409, json: { detail: { code: "MANIFEST_HASH_MISMATCH", message: "manifest changed since review", write_occurred: false } } });
  });
  await page.route(`**/api/corpus/batches/${batchId}/ingest`, async (route) => {
    ingestCount += 1;
    await route.fulfill({ json: { status: "ok" } });
  });

  await page.goto("/operator.html");
  await page.getByRole("button", { name: "Inspect", exact: true }).click();
  await page.getByRole("button", { name: "Authorize and Ingest", exact: true }).click();

  await expect(page.getByRole("alert")).toContainText("MANIFEST_HASH_MISMATCH");
  expect(authorizeCount).toBe(1);
  expect(ingestCount).toBe(0);
});

test("review refreshes stale batch state before authorization", async ({ page }) => {
  const batchId = "66666666-6666-4666-8666-666666666666";
  const status = { value: "SCANNED" };
  const batch = reviewedBatch(batchId, "SCANNED");
  await page.route(`**/api/projects/${projectId}/corpus/batches`, (route) => route.fulfill({ json: { batches: [{ ...batch, status: status.value }] } }));
  await page.route(`**/api/corpus/batches/${batchId}`, (route) => route.fulfill({ json: { status: "ok", batch: { ...batch, status: status.value }, manifest_sha256: "a".repeat(64), authorization_present: false } }));
  let reviewCount = 0;
  await page.route(`**/api/corpus/batches/${batchId}/review`, async (route) => {
    reviewCount += 1;
    status.value = "PENDING_HUMAN_AUTHORIZATION";
    await route.fulfill({ json: { status: "ok", batch: reviewedBatch(batchId), manifest_sha256: "a".repeat(64) } });
  });

  await page.goto("/operator.html");
  await page.getByRole("button", { name: "Inspect", exact: true }).click();
  await page.getByRole("button", { name: "Review", exact: true }).click();

  await expect(page.getByText("PENDING HUMAN AUTHORIZATION", { exact: true }).last()).toBeVisible();
  await expect(page.getByRole("button", { name: "Authorize", exact: true })).toBeEnabled();
  expect(reviewCount).toBe(1);
});

test("backend forbidden authorization is surfaced to the operator", async ({ page }) => {
  const batchId = "77777777-7777-4777-8777-777777777777";
  const status = { value: "PENDING_HUMAN_AUTHORIZATION" };
  await routeReviewedBatch(page, batchId, status);
  await page.route(`**/api/corpus/batches/${batchId}/authorize`, (route) => route.fulfill({ status: 403, json: { detail: { code: "LOCAL_ONLY", message: "Corpus operator API is local-only", write_occurred: false } } }));

  await page.goto("/operator.html");
  await page.getByRole("button", { name: "Inspect", exact: true }).click();
  await page.getByRole("button", { name: "Authorize", exact: true }).click();

  await expect(page.getByRole("alert")).toContainText("LOCAL_ONLY");
});
