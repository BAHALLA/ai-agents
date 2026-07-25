/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { API_PREFIXES } from "./src/api/paths";

// The console is served same-origin by the FastAPI front door in production
// (StaticFiles from serving/static). During local dev we proxy API calls to a
// locally running server so the browser has no CORS or auth-origin surprises.
// Point VITE_DEV_API_TARGET at your `make run-assistant-api` instance.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // Emitted bundle is copied into the Python image at build time. Keep it
    // deterministic and fail the build on anything unexpectedly large.
    outDir: "dist",
    sourcemap: true,
    chunkSizeWarningLimit: 700,
  },
  server: {
    port: 5173,
    // Derived from the shared prefix list rather than repeated here — this list
    // drifted once already (/me and /onboarding were missing, so the System
    // pane's checks 404'd in dev while :8000 served them fine). A test in
    // src/api/client.test.ts fails if an ApiClient path escapes the list.
    proxy: Object.fromEntries(
      API_PREFIXES.map((path) => [
        path,
        {
          target: process.env.VITE_DEV_API_TARGET ?? "http://localhost:8000",
          changeOrigin: true,
        },
      ]),
    ),
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    coverage: {
      provider: "v8",
      reporter: ["text", "html"],
      include: ["src/**/*.{ts,tsx}"],
      exclude: ["src/**/*.test.{ts,tsx}", "src/test/**", "src/main.tsx", "src/vite-env.d.ts"],
    },
  },
});
