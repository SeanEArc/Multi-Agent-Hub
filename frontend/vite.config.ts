import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The backend creates this file on first start (see backend/app/auth.py).
const tokenFile =
  process.env.HUB_TOKEN_FILE ?? fileURLToPath(new URL("../backend/data/token", import.meta.url));

// Read on every request, so a token created after Vite started is still picked up.
function readToken(): string | null {
  try {
    return readFileSync(tokenFile, "utf-8").trim() || null;
  } catch {
    return null;
  }
}

export default defineConfig({
  plugins: [react()],
  server: {
    // Same loopback address as the backend; plain "localhost" can bind IPv6 only on Windows.
    host: "127.0.0.1",
    port: 5173,
    // The UI calls /api on its own origin; Vite forwards it to the FastAPI backend and adds
    // the API token, so the browser never sees it.
    proxy: {
      "/api": {
        target: process.env.HUB_API_URL ?? "http://127.0.0.1:8765",
        // Send the backend's own Host, which its loopback Host check accepts.
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq) => {
            const token = readToken();
            if (token) proxyReq.setHeader("Authorization", `Bearer ${token}`);
            else proxyReq.removeHeader("Authorization");
          });
        },
      },
    },
  },
});
