/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The console is served same-origin by the FastAPI front door in production
// (StaticFiles from serving/static). During local dev we proxy API calls to a
// locally running server so the browser has no CORS or auth-origin surprises.
// Point VITE_DEV_API_TARGET at your `make run-assistant-api` instance.
export default defineConfig({
  plugins: [react()],
  build: {
    // Emitted bundle is copied into the Python image at build time. Keep it
    // deterministic and fail the build on anything unexpectedly large.
    outDir: "dist",
    sourcemap: true,
    chunkSizeWarningLimit: 700,
  },
  server: {
    port: 5173,
    proxy: {
      "/chat": {
        target: process.env.VITE_DEV_API_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
      },
      "/healthz": {
        target: process.env.VITE_DEV_API_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
      },
      "/readyz": {
        target: process.env.VITE_DEV_API_TARGET ?? "http://localhost:8000",
        changeOrigin: true,
      },
    },
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
