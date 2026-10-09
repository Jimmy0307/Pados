import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode, command }) => {
  const env = loadEnv(mode, ".", "PADOS_");
  return {
    base: command === "build" ? env.PADOS_PAGES_BASE || "/Pados/" : "/",
    plugins: [react()],
    build: {
      rollupOptions: {
        input: { main: "index.html", remote: "remote.html", operator: "operator.html" },
      },
    },
    server: {
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
      },
    },
  };
});
