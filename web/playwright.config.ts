import { defineConfig, devices } from "@playwright/test";

// PADOS_WEB_PORT moves the Vite dev server (vite.config.ts reads the same variable) when 5173 is taken; unset, 5173.
const webPort = Number(process.env.PADOS_WEB_PORT || 5173);
const webUrl = `http://127.0.0.1:${webPort}`;

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  fullyParallel: true,
  reporter: [["list"], ["html", { outputFolder: "playwright-report", open: "never" }]],
  use: {
    baseURL: webUrl,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: {
    command: "npm run dev -- --host 127.0.0.1",
    url: webUrl,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
