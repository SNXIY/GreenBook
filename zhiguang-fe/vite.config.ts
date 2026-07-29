import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8080",
        changeOrigin: true
      },
      "/creator-agent": {
        target: "http://127.0.0.1:8092",
        changeOrigin: true,
        rewrite: path => path.replace(/^\/creator-agent/, "")
      },
      "/assistant-agent": {
        target: "http://127.0.0.1:8094",
        changeOrigin: true,
        rewrite: path => path.replace(/^\/assistant-agent/, "")
      }
    }
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url))
    }
  }
});
