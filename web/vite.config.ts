import { defineConfig } from "vite";

export default defineConfig({
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 1200 },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:18443" },
      "/ws": { target: "ws://127.0.0.1:18443", ws: true },
    },
  },
});
