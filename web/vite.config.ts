import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { ENTRY_MODULES, HTML_ENTRIES, PREBUNDLED_DEPS, devReadinessPlugin } from "./vite-dev-readiness";

export default defineConfig(({ mode, command }) => {
  const env = loadEnv(mode, ".", "PADOS_");
  return {
    base: command === "build" ? env.PADOS_PAGES_BASE || "/Pados/" : "/",
    plugins: [react(), devReadinessPlugin()],
    build: {
      rollupOptions: {
        input: HTML_ENTRIES,
      },
    },
    // A deterministic start: crawl all three pages and pre-bundle every dependency before the first page load, so a
    // dependency found late can never trigger a "new dependencies optimized ... reloading" full reload while a
    // request (a single-use pairing code) is in flight. src/dev-server-config.test.ts keeps these lists complete.
    optimizeDeps: {
      entries: Object.values(HTML_ENTRIES),
      include: [...PREBUNDLED_DEPS],
    },
    server: {
      // Transform the entry modules at startup; GET /__pados/dev-ready (vite-dev-readiness.ts) waits for this work.
      warmup: { clientFiles: ENTRY_MODULES.map((path) => `.${path}`) },
      host: "127.0.0.1",
      port: Number(env.PADOS_WEB_PORT || 5173),
      proxy: {
        "/remote/v1": {
          target: env.PADOS_WEB_REMOTE_URL || env.PADOS_WEB_BACKEND_URL || "http://127.0.0.1:8088",
          changeOrigin: false,
        },
        "/api": {
          target: env.PADOS_WEB_BACKEND_URL || "http://127.0.0.1:8088",
          changeOrigin: false,
        },
        // The Control Center's work request and approval actions use the existing control API with the token.
        "/control": {
          target: env.PADOS_WEB_BACKEND_URL || "http://127.0.0.1:8088",
          changeOrigin: false,
        },
        // Browser pairing and the paired session (docs/identity): proxied unchanged, so the page stays same-origin and
        // the session cookie, the Origin header and X-PADOS-CSRF reach the API as the browser sent them. No CORS.
        "/identity": {
          target: env.PADOS_WEB_BACKEND_URL || "http://127.0.0.1:8088",
          changeOrigin: false,
        },
      },
    },
  };
});
