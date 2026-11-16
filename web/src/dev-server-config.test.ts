// SPDX-License-Identifier: AGPL-3.0-only
// The dev server starts deterministically: the optimizer pre-bundles every dependency of all three pages before the
// first page load (no "new dependencies optimized ... reloading" while a request is in flight), the entry modules are
// warmed up, and GET /__pados/dev-ready answers only once that work is done (the startup readiness condition).
import type { UserConfig } from "vite";
import { describe, expect, it, vi } from "vitest";
import viteConfig from "../vite.config";
import indexHtml from "../index.html?raw";
import operatorHtml from "../operator.html?raw";
import remoteHtml from "../remote.html?raw";
import {
  DEV_READY_PATH, ENTRY_MODULES, HTML_ENTRIES, PREBUNDLED_DEPS, devReadiness, devReadinessPlugin, type ClientEnvironment,
} from "../vite-dev-readiness";

type ConfigFn = (env: { mode: string; command: "serve" | "build"; isSsrBuild?: boolean; isPreview?: boolean }) => UserConfig;
const serve = (viteConfig as unknown as ConfigFn)({ mode: "development", command: "serve" });
const sources = import.meta.glob(["./**/*.ts", "./**/*.tsx", "!./**/*.test.ts", "!./**/*.test.tsx"],
  { query: "?raw", import: "default", eager: true }) as Record<string, string>;

function bareImports(source: string): string[] {
  const found = [...source.matchAll(/(?:import|export)\s[^;]*?from\s+["']([^"'./][^"']*)["']|import\(\s*["']([^"'./][^"']*)["']\s*\)/g)];
  return found.map((match) => match[1] || match[2]);
}

describe("dev server optimizer configuration", () => {
  it("crawls exactly the three HTML pages that the build uses", () => {
    expect(serve.build?.rollupOptions?.input).toEqual(HTML_ENTRIES);
    expect(serve.optimizeDeps?.entries).toEqual(Object.values(HTML_ENTRIES));
  });

  it("pre-bundles every bare import of the app before the first page", () => {
    const imported = new Set(Object.values(sources).flatMap(bareImports));
    expect(imported.size).toBeGreaterThan(0);
    expect(serve.optimizeDeps?.include).toEqual([...PREBUNDLED_DEPS]);
    for (const dep of imported) expect(PREBUNDLED_DEPS, `${dep} is imported by src/ but not pre-bundled`).toContain(dep);
    expect(PREBUNDLED_DEPS).toContain("react/jsx-dev-runtime");  // the JSX transform's import
  });

  it("warms up the module script of every page, and nothing else", () => {
    const scripts = [indexHtml, remoteHtml, operatorHtml].flatMap((html) =>
      [...html.matchAll(/<script\b[^>]*type="module"[^>]*src="([^"]+)"/g)].map((match) => match[1]));
    expect(new Set(scripts)).toEqual(new Set(ENTRY_MODULES));
    expect(serve.server?.warmup?.clientFiles).toEqual(ENTRY_MODULES.map((path) => `.${path}`));
  });

  it("serves the readiness endpoint in the dev server only", () => {
    const names = (serve.plugins || []).flat().map((plugin) => (plugin && typeof plugin === "object" && "name" in plugin ? plugin.name : ""));
    expect(names).toContain("pados-dev-readiness");
    expect(devReadinessPlugin().apply).toBe("serve");
  });
});

function deferred(): { promise: Promise<void>; resolve: () => void } {
  let resolve = () => {};
  const promise = new Promise<void>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("devReadiness", () => {
  it("answers only after the entries are transformed and every optimizer run has finished", async () => {
    const scan = deferred();
    const bundle = deferred();
    const late = deferred();
    const optimized: Record<string, { processing?: Promise<void> }> = { react: { processing: bundle.promise } };
    const discovered: Record<string, { processing?: Promise<void> }> = {};
    const warmed: string[] = [];
    const env: ClientEnvironment = {
      warmupRequest: vi.fn(async (url: string) => { warmed.push(url); }),
      waitForRequestsIdle: vi.fn(async () => {}),
      depsOptimizer: { scanProcessing: scan.promise, metadata: { optimized, discovered } },
    };
    let settled = false;
    const report = devReadiness(env).then((value) => { settled = true; return value; });
    await Promise.resolve();
    expect(warmed).toEqual([...ENTRY_MODULES]);
    scan.resolve();
    await new Promise((done) => setTimeout(done, 0));
    expect(settled).toBe(false);  // the bundle is still running
    // The bundle run discovers one more dependency (a second run starts) before it finishes.
    discovered["react-dom/client"] = { processing: late.promise };
    bundle.resolve();
    await new Promise((done) => setTimeout(done, 0));
    expect(settled).toBe(false);  // the second run is still running
    late.resolve();
    optimized["react-dom/client"] = {};
    delete discovered["react-dom/client"];
    expect(await report).toEqual({ ready: true, entryModules: [...ENTRY_MODULES], optimizedDeps: ["react", "react-dom/client"] });
  });

  it("works without an optimizer (deps optimization disabled)", async () => {
    const env: ClientEnvironment = { warmupRequest: async () => {}, waitForRequestsIdle: async () => {} };
    expect(await devReadiness(env, ["/src/main.tsx"])).toEqual({ ready: true, entryModules: ["/src/main.tsx"], optimizedDeps: [] });
  });
});

type Handler = (request: { method?: string }, response: FakeResponse) => void;
type FakeResponse = { statusCode: number; headers: Record<string, string>; body: string; done: Promise<void>;
  setHeader(name: string, value: string): void; end(body?: string): void };

function fakeResponse(): FakeResponse {
  let finish = () => {};
  const done = new Promise<void>((resolve) => { finish = resolve; });
  return { statusCode: 0, headers: {}, body: "", done, setHeader(name, value) { this.headers[name] = value; },
    end(body = "") { this.body = body; finish(); } };
}

function mount(client: ClientEnvironment): { path: string; handler: Handler } {
  let mounted: { path: string; handler: Handler } | null = null;
  const server = { environments: { client }, middlewares: { use: (path: string, handler: Handler) => { mounted = { path, handler }; } } };
  const hook = devReadinessPlugin().configureServer as unknown as (server: unknown) => void;
  hook(server);
  if (!mounted) throw new Error("no middleware");
  return mounted;
}

describe("GET /__pados/dev-ready", () => {
  const ready: ClientEnvironment = { warmupRequest: async () => {}, waitForRequestsIdle: async () => {} };

  it("answers 200 with the report once ready, never cached", async () => {
    const { path, handler } = mount(ready);
    expect(path).toBe(DEV_READY_PATH);
    const response = fakeResponse();
    handler({ method: "GET" }, response);
    await response.done;
    expect(response.statusCode).toBe(200);
    expect(response.headers["Cache-Control"]).toBe("no-store");
    expect(JSON.parse(response.body)).toMatchObject({ ready: true });
  });

  it("answers 503 with the reason when the warm-up fails, and 405 to a write", async () => {
    const broken: ClientEnvironment = { warmupRequest: async () => { throw new Error("transform failed"); }, waitForRequestsIdle: async () => {} };
    const failing = fakeResponse();
    mount(broken).handler({ method: "GET" }, failing);
    await failing.done;
    expect(failing.statusCode).toBe(503);
    expect(JSON.parse(failing.body)).toEqual({ ready: false, reason: "transform failed" });
    const write = fakeResponse();
    mount(ready).handler({ method: "POST" }, write);
    await write.done;
    expect(write.statusCode).toBe(405);
  });
});
