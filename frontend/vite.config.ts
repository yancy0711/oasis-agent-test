import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    // MapLibre is already isolated and lazy-loaded; its minified vendor bundle is about 1 MB.
    chunkSizeWarningLimit: 1100,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom"],
          query: ["@tanstack/react-query", "zustand"],
          report: ["react-markdown", "katex", "rehype-katex", "remark-math"],
          maplibre: ["maplibre-gl"],
        },
      },
    },
  },
  server: {
    port: 5173,
    allowedHosts: [".trycloudflare.com", "localhost"],
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
  },
});
