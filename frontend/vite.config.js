import { createRequire } from "node:module";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const pkg = createRequire(import.meta.url)("./package.json");

// The dev server proxies API calls to the FastAPI backend on :8000 so cookies are
// same-origin during development (no CORS complications for the secure session flow).
export default defineConfig({
  plugins: [react()],
  // Stamped into handoff exports as workspace provenance.
  define: { __APP_VERSION__: JSON.stringify(pkg.version) },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:8000", changeOrigin: true },
      // Proxied so the filter-capability probe works in dev too.
      "/openapi.json": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.js"],
    include: ["tests/**/*.test.{js,jsx}"],
    css: false,
  },
});
