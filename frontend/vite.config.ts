import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    // Same loopback address as the backend; plain "localhost" can bind IPv6 only on Windows.
    host: "127.0.0.1",
    port: 5173,
    // The UI calls /api on its own origin; Vite forwards it to the FastAPI backend.
    proxy: {
      "/api": process.env.HUB_API_URL ?? "http://127.0.0.1:8765",
    },
  },
});
