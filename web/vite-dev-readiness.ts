// SPDX-License-Identifier: AGPL-3.0-only
// The Vite dev server's startup readiness condition. A dev server answers `/` with 200 in a few hundred
// milliseconds, before it has scanned and pre-bundled the dependencies or transformed a single entry module; a page
// opened in that window does that work itself, and a dependency the scanner missed then triggers a full page reload
// ("new dependencies optimized ... reloading") that kills any request in flight. GET /__pados/dev-ready answers only
// once every entry module (and its static imports) has been transformed and the optimizer has finished, so a test
// driver or a launcher that waits on it opens a page on a server with no pending optimizer work.
import type { Plugin } from "vite";

export const DEV_READY_PATH = "/__pados/dev-ready";
/** The three pages; the same list feeds build.rollupOptions.input and optimizeDeps.entries. */
export const HTML_ENTRIES = { main: "index.html", remote: "remote.html", operator: "operator.html" } as const;
/** The module scripts those pages load (a config test checks the HTML files against this list). */
export const ENTRY_MODULES = ["/src/main.tsx", "/src/remote-main.tsx"] as const;
/** Every bare import of the app (a config test checks src/ against this list), pre-bundled before the first page. */
export const PREBUNDLED_DEPS = ["react", "react/jsx-dev-runtime", "react-dom/client"] as const;

export type DevReadiness = { ready: true; entryModules: string[]; optimizedDeps: string[] };

type OptimizerInfo = { processing?: Promise<void> };
/** The parts of Vite's client DevEnvironment the condition needs (a structural type keeps it testable). */
export type ClientEnvironment = {
  warmupRequest(url: string): Promise<void>;
  waitForRequestsIdle(ignoredId?: string): Promise<void>;
  depsOptimizer?: {
    scanProcessing?: Promise<void>;
    metadata: { optimized: Record<string, OptimizerInfo>; discovered: Record<string, OptimizerInfo> };
  };
};

const MAX_OPTIMIZER_ROUNDS = 20;

/** Resolves when the entry modules are transformed and the optimizer has no scan or bundle in progress. */
export async function devReadiness(env: ClientEnvironment, entryModules: readonly string[] = ENTRY_MODULES): Promise<DevReadiness> {
  await Promise.all(entryModules.map((url) => env.warmupRequest(url)));
  await env.waitForRequestsIdle();
  const optimizer = env.depsOptimizer;
  if (optimizer) {
    await optimizer.scanProcessing;
    // A bundle run can discover more dependencies and start another: wait until a round adds nothing new.
    for (let round = 0; round < MAX_OPTIMIZER_ROUNDS; round += 1) {
      const pending = [...Object.values(optimizer.metadata.optimized), ...Object.values(optimizer.metadata.discovered)];
      const before = pending.length;
      await Promise.all(pending.map((info) => info.processing));
      await env.waitForRequestsIdle();
      const after = Object.keys(optimizer.metadata.optimized).length + Object.keys(optimizer.metadata.discovered).length;
      if (after === before) break;
    }
  }
  return { ready: true, entryModules: [...entryModules], optimizedDeps: Object.keys(optimizer?.metadata.optimized ?? {}).sort() };
}

/** Serves GET /__pados/dev-ready (dev server only) with the readiness report, or 503 with the reason. */
export function devReadinessPlugin(entryModules: readonly string[] = ENTRY_MODULES): Plugin {
  return {
    name: "pados-dev-readiness",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use(DEV_READY_PATH, (request, response) => {
        if (request.method !== "GET" && request.method !== "HEAD") {
          response.statusCode = 405;
          response.end();
          return;
        }
        devReadiness(server.environments.client as unknown as ClientEnvironment, entryModules).then((report) => {
          response.statusCode = 200;
          response.setHeader("Content-Type", "application/json");
          response.setHeader("Cache-Control", "no-store");
          response.end(JSON.stringify(report));
        }, (error: unknown) => {
          response.statusCode = 503;
          response.setHeader("Content-Type", "application/json");
          response.end(JSON.stringify({ ready: false, reason: error instanceof Error ? error.message : String(error) }));
        });
      });
    },
  };
}
