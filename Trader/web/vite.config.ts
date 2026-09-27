import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The API (uvicorn, `python -m trader.api`) listens on 127.0.0.1:8000 in development.
const API_TARGET = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: false,
        configure: (proxy) => {
          // Server-sent events must reach the browser as they are written: ask every hop not to
          // buffer or transform the stream.
          proxy.on("proxyRes", (res) => {
            const type = res.headers["content-type"];
            if (typeof type === "string" && type.startsWith("text/event-stream")) {
              res.headers["cache-control"] = "no-cache, no-transform";
              res.headers["x-accel-buffering"] = "no";
            }
          });
        },
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
