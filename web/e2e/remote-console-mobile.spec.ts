import { createHash, generateKeyPairSync, randomBytes, verify as verifySignature } from "node:crypto";
import { expect, test, devices, type Page } from "@playwright/test";

test.use({ ...devices["Pixel 7"] });

const attention = {
  needs_attention: [{ run_id: "run-w", project_id: "proj-a", goal: "Tighten README", bucket: "WAITING_APPROVAL",
    progress_status: "VERIFIED", semantic_commit_status: "NOT_COMMITTED", approval_ref: "appr-1" }],
  running: [{ run_id: "run-r", project_id: "proj-a", goal: "Index docs", bucket: "RUNNING", progress_status: "WORKING" }],
  waiting_approval: [{ run_id: "run-w", project_id: "proj-a", goal: "Tighten README", bucket: "WAITING_APPROVAL",
    progress_status: "VERIFIED", semantic_commit_status: "NOT_COMMITTED", approval_ref: "appr-1" }],
  failed: [], completed: [], projection: "PADOS_READ_MODEL_NOT_AUTHORITY",
};
const workMap = {
  projects: ["proj-a"],
  tasks: [{ request_id: "request-1", task_id: "task-1", project_id: "proj-a", original_intent: "Prepare launch memo",
    classification: "INTERNAL", priority: "NORMAL", deadline: null, scope_version_ref: "scope:4", decomposition_status: "PENDING",
    work_request_status: "PENDING_WORKER_ROUTER", created_at: "2026-10-05T00:00:00Z",
    goals: [{ goal_id: "goal-1", title: "Prepare launch memo", objective: "Prepare launch memo", state: "PENDING", required_capabilities: [], dependencies: [] }],
    assignments: [], artifacts: [], evidence: [], receipts: [] }],
  runs: [], projection: "REBUILDABLE_PADOS_PROJECTION_NOT_AUTHORITY", semantic_authority: "PADOS_HOME_BASE",
};
let workMapTasks = [...workMap.tasks];

const b64url = (value: Buffer) => value.toString("base64url");

/** Chromium virtual authenticator (CDP): a real WebAuthn ceremony with user verification. */
async function virtualAuthenticator(page: Page) {
  const keys = generateKeyPairSync("ec", { namedCurve: "P-256" });
  const keyHandle = randomBytes(16);
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  const { authenticatorId } = await cdp.send("WebAuthn.addVirtualAuthenticator", { options: {
    protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true,
    isUserVerified: true, automaticPresenceSimulation: true } });
  await cdp.send("WebAuthn.addCredential", { authenticatorId, credential: {
    credentialId: keyHandle.toString("base64"), isResidentCredential: false, rpId: "localhost",
    privateKey: (keys.privateKey.export({ format: "der", type: "pkcs8" }) as Buffer).toString("base64"), signCount: 0 } });
  return { keyHandle, publicKey: keys.publicKey };
}

test("mobile remote console: sign in, attention, approve, send intent", async ({ page }) => {
  const posted: { url: string; body: unknown; headers: Record<string, string> }[] = [];
  let decided = false;
  const authenticator = await virtualAuthenticator(page);
  const nonce = randomBytes(32);
  let presenceVerified = false;
  const dialogs: string[] = [];
  page.on("dialog", (dialog) => { dialogs.push(dialog.message()); void dialog.accept(); });
  await page.route("**/remote/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === "POST") {
      const body = (request.postDataJSON() || {}) as Record<string, unknown>;
      posted.push({ url: url.pathname, body, headers: request.headers() });
      if (url.pathname === "/remote/v1/sessions") return route.fulfill({ json: { session_handle: "s".repeat(43) } });
      if (url.pathname.endsWith("/step-up")) {
        return route.fulfill({ json: { challenge_id: "c-1", expires_at: "2026-10-07T00:00:00Z", binding_digest: "b".repeat(64),
          binding: { action: "APPROVE", project_id: "proj-a", task_id: "run-w", goal_id: null, candidate_id: "run-w",
            candidate_digest: "d".repeat(64), classification: "INTERNAL", expected_scope_version: 4, kind: "PRODUCT_RUN" },
          public_key: { challenge: b64url(nonce), rpId: "localhost", timeout: 60000, userVerification: "required",
            allowCredentials: [{ type: "public-key", id: b64url(authenticator.keyHandle) }] } } });
      }
      if (url.pathname.endsWith("/verify")) {
        // The mock Home Base checks the genuine assertion: challenge, origin, UV flag and ES256 signature.
        const assertion = body.assertion as Record<string, string>;
        const clientData = Buffer.from(assertion.client_data_json, "base64url");
        const authData = Buffer.from(assertion.authenticator_data, "base64url");
        const parsed = JSON.parse(clientData.toString("utf8")) as Record<string, string>;
        const signed = Buffer.concat([authData, createHash("sha256").update(clientData).digest()]);
        presenceVerified = parsed.type === "webauthn.get" && parsed.challenge === b64url(nonce)
          && parsed.origin === "http://localhost:5173" && (authData[32] & 0x05) === 0x05
          && verifySignature("sha256", signed, authenticator.publicKey, Buffer.from(assertion.signature, "base64url"));
        return presenceVerified ? route.fulfill({ json: { proof_ref: "step-up-proof:1", single_use: true } })
          : route.fulfill({ status: 403, json: { detail: "APPROVAL_PRESENCE_SIGNATURE_INVALID" } });
      }
      if (url.pathname.endsWith("/approve")) decided = body.step_up_proof === "step-up-proof:1";
      if (url.pathname === "/remote/v1/intents") {
        workMapTasks = [...workMapTasks, {
          request_id: "request-created-1", task_id: "task-created-1", project_id: String(body.project_id),
          original_intent: String(body.human_message), classification: "INTERNAL", priority: String(body.priority),
          deadline: body.deadline as string, scope_version_ref: "scope:4", decomposition_status: "PENDING",
          work_request_status: "PENDING_WORKER_ROUTER", created_at: "2026-10-05T00:00:00Z",
          goals: [{ goal_id: "goal-created-1", title: String(body.human_message), objective: String(body.human_message),
            state: "PENDING", required_capabilities: [], dependencies: [] }],
          assignments: [], artifacts: [], evidence: [], receipts: [],
        }];
      }
      return route.fulfill({ json: { intent_id: "x", outcome: url.pathname.endsWith("/approve") ? "COMMITTED" : "SUBMITTED", ledger_entry_hash: "h" } });
    }
    if (url.pathname === "/remote/v1/attention") {
      return route.fulfill({ json: decided ? { ...attention, needs_attention: [], waiting_approval: [] } : attention });
    }
    if (url.pathname === "/remote/v1/work-map") return route.fulfill({ json: { ...workMap, tasks: workMapTasks } });
    if (url.pathname === "/remote/v1/nodes") {
      return route.fulfill({ json: { status: "COMPUTE_NODES_CONFIGURED", model_endpoints_exposed: false,
        nodes: [{ node_id: "n1", node_name: "rtx-node", state: "ONLINE", stale: false, authorized: true }] } });
    }
    return route.fulfill({ status: 404, json: { detail: "not mocked" } });
  });

  // WebAuthn needs a domain origin (not a bare IP), so this test uses localhost.
  await page.goto("http://localhost:5173/");
  await page.getByLabel("Actor").fill("owner");
  await page.getByLabel("Access proof").fill("synthetic-proof");
  await page.getByLabel("Confirm Home Base origin").check();
  await page.getByRole("button", { name: "Connect" }).click();

  await page.getByRole("navigation", { name: "PADOS views" }).getByRole("button", { name: "Attention" }).click();
  await expect(page.getByRole("region", { name: "Needs attention" })).toContainText("Tighten README");
  await page.getByRole("navigation", { name: "PADOS views" }).getByRole("button", { name: "Map" }).click();
  await page.getByRole("button", { name: "Zoom in" }).click();
  await expect(page.getByText("110%", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Collapse goals" }).click();
  await expect(page.getByRole("button", { name: /GOAL · PENDING/ })).toHaveCount(0);
  await page.getByRole("button", { name: "Expand goals" }).click();
  await page.getByRole("button", { name: /ORIGINAL TASK.*Prepare launch memo/ }).click();
  await expect(page.getByRole("complementary", { name: "Work details" })).toContainText("scope:4");
  await page.getByRole("button", { name: "Close details" }).click();
  const box = await page.getByRole("navigation", { name: "PADOS views" }).boundingBox();
  expect(box && box.width).toBeLessThanOrEqual(page.viewportSize()!.width);

  await page.getByRole("navigation", { name: "PADOS views" }).getByRole("button", { name: "Actions" }).click();
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByRole("status")).toContainText("approve: COMMITTED");
  expect(posted.map((item) => item.url)).toEqual(["/remote/v1/sessions", "/remote/v1/approvals/appr-1/step-up",
    "/remote/v1/approvals/appr-1/step-up/c-1/verify", "/remote/v1/approvals/appr-1/approve"]);
  expect(presenceVerified).toBe(true);
  expect(dialogs[0]).toContain("dddddddddddddddd");
  expect(posted[3].headers["x-pados-remote-actor"]).toBe("owner");
  expect(posted[3].headers["x-pados-remote-session"]).toBe("s".repeat(43));
  expect((posted[3].body as Record<string, unknown>).step_up_proof).toBe("step-up-proof:1");
  expect(Object.keys(posted[1].body as Record<string, unknown>)).toEqual(["action"]);

  await page.getByRole("navigation", { name: "PADOS views" }).getByRole("button", { name: "Tasks" }).click();
  await page.getByLabel("Project").selectOption("proj-a");
  await page.getByLabel("Original intent").fill("summarise status");
  await page.getByLabel("Priority").selectOption("HIGH");
  await page.getByLabel("Deadline").fill("2026-10-06T09:30");
  await page.getByRole("button", { name: "Create task" }).click();
  await expect(page.getByRole("status")).toContainText("create task: SUBMITTED");
  await expect(page.getByRole("region", { name: "Tasks" })).toContainText("summarise status");
  const intent = posted[4].body as Record<string, unknown>;
  expect(Object.keys(intent).sort()).toEqual(["deadline", "human_message", "intent_id", "intent_type", "priority", "project_id"]);
  expect(intent.priority).toBe("HIGH");
  expect(intent.deadline).toMatch(/Z$/);

  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
  expect(stored).not.toContain("synthetic-proof");
});

test("mobile offline state never reports an unavailable Home Base as successful", async ({ page }) => {
  await page.route("**/remote/v1/**", (route) => route.abort("internetdisconnected"));
  await page.goto("/");
  await page.getByLabel("Actor").fill("owner");
  await page.getByLabel("Access proof").fill("synthetic-proof");
  await page.getByLabel("Confirm Home Base origin").check();
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.getByRole("alert")).toContainText("OFFLINE · REMOTE UNREACHABLE");
  await expect(page.getByRole("status")).toHaveCount(0);
  const stored = await page.evaluate(() => JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage }));
  expect(stored).not.toContain("synthetic-proof");
  const cachedAuthorityPayload = await page.evaluate(async () => {
    await navigator.serviceWorker.ready;
    const names = await caches.keys();
    for (const name of names) {
      const cache = await caches.open(name);
      if (await cache.match("/remote/v1/work-map")) return true;
      if (await cache.match("/remote/v1/attention")) return true;
    }
    return false;
  });
  expect(cachedAuthorityPayload).toBe(false);
});

test("refused task submission preserves the original intent for retry", async ({ page }) => {
  workMapTasks = [...workMap.tasks];
  await page.route("**/remote/v1/**", async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "POST" && url.pathname === "/remote/v1/intents") {
      return route.fulfill({ status: 403, json: { detail: "TASK_REFUSED" } });
    }
    if (url.pathname === "/remote/v1/attention") return route.fulfill({ json: attention });
    if (url.pathname === "/remote/v1/work-map") return route.fulfill({ json: { ...workMap, tasks: workMapTasks } });
    if (url.pathname === "/remote/v1/nodes") return route.fulfill({ json: { status: "COMPUTE_NODES_CONFIGURED", nodes: [] } });
    return route.fulfill({ status: 404, json: { detail: "not mocked" } });
  });

  await page.goto("/");
  await page.getByLabel("Actor").fill("owner");
  await page.getByLabel("Access proof").fill("synthetic-proof");
  await page.getByLabel("Confirm Home Base origin").check();
  await page.getByRole("button", { name: "Connect" }).click();
  await page.getByRole("navigation", { name: "PADOS views" }).getByRole("button", { name: "Tasks" }).click();
  await page.getByLabel("Project").selectOption("proj-a");
  await page.getByLabel("Original intent").fill("retain this task if Home Base refuses it");
  await page.getByRole("button", { name: "Create task" }).click();

  await expect(page.getByRole("status")).toContainText("create task: TASK_REFUSED");
  await expect(page.getByLabel("Original intent")).toHaveValue("retain this task if Home Base refuses it");
});

test("the access proof is not sent before the Home Base origin is confirmed", async ({ page }) => {
  let requests = 0;
  await page.route("**/remote/v1/**", async (route) => {
    requests += 1;
    const url = new URL(route.request().url());
    if (url.pathname === "/remote/v1/attention") return route.fulfill({ json: attention });
    if (url.pathname === "/remote/v1/work-map") return route.fulfill({ json: workMap });
    if (url.pathname === "/remote/v1/nodes") return route.fulfill({ json: { status: "OK", nodes: [] } });
    return route.fulfill({ json: {} });
  });

  await page.goto("/");
  await page.getByLabel("Actor").fill("owner");
  await page.getByLabel("Access proof").fill("synthetic-proof");
  await page.getByRole("button", { name: "Connect" }).click();

  await expect(page.getByRole("alert")).toContainText("Confirm that the selected origin");
  expect(requests).toBe(0);
  await page.getByLabel("Confirm Home Base origin").check();
  await page.getByRole("button", { name: "Connect" }).click();
  await expect(page.getByText("HOME BASE ONLINE / REMOTE CONNECTED")).toBeVisible();
  expect(requests).toBeGreaterThan(0);
});
