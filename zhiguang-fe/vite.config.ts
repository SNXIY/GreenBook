import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

export default defineConfig(({ mode }) => {
  const envDir = fileURLToPath(new URL("../", import.meta.url));
  const env = loadEnv(mode, envDir, "");
  const agentTarget =
    env.VITE_GREENBOOK_AGENT_PROXY_TARGET || "http://127.0.0.1:8094";

  console.info(`[vite] GreenBook Agent proxy target: ${agentTarget}`);

  return {
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8080",
        changeOrigin: true
      },
      "/agent-api": {
        target: agentTarget,
        changeOrigin: true,
        rewrite: path => path.replace(/^\/agent-api/, "")
      }
    }
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url))
    }
  }
  };
});
