import { readFileSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig, type Plugin } from "vite";

// The backend creates this file on first start (see backend/app/auth.py and config.py).
const appData = process.env.LOCALAPPDATA ?? join(homedir(), ".local", "share");
const tokenFile = process.env.HUB_TOKEN_FILE ?? join(appData, "MultiAgentHub", "data", "token");

// Read on every request, so a token created after Vite started is still picked up.
function readToken(): string | null {
  try {
    return readFileSync(tokenFile, "utf-8").trim() || null;
  } catch {
    return null;
  }
}

// pdf.js data it fetches at run time (CJK character maps, the standard fonts, its JPEG 2000 / JBIG2 / colour WASM
// decoders and ICC profiles), served from the app's own origin under /pdfjs/ so the preview never goes online.
// QuickJS (PDF scripting, never enabled) is left out.
const PDFJS = join(__dirname, "node_modules", "pdfjs-dist");
const PDFJS_FOLDERS = ["cmaps", "standard_fonts", "wasm", "iccs"];
const pdfjsFiles = () =>
  PDFJS_FOLDERS.flatMap((folder) =>
    readdirSync(join(PDFJS, folder))
      .filter((name) => !name.startsWith("quickjs"))
      .map((name) => `${folder}/${name}`),
  );

function pdfjsAssets(): Plugin {
  return {
    name: "pdfjs-assets",
    configureServer(server) {
      const files = new Set(pdfjsFiles());
      server.middlewares.use("/pdfjs/", (req, res, next) => {
        const file = decodeURIComponent((req.url ?? "").split("?")[0].replace(/^\//, ""));
        if (!files.has(file)) return next();
        if (file.endsWith(".wasm")) res.setHeader("Content-Type", "application/wasm");
        else if (file.endsWith(".js")) res.setHeader("Content-Type", "text/javascript");
        res.end(readFileSync(join(PDFJS, file)));
      });
    },
    generateBundle() {
      for (const file of pdfjsFiles()) this.emitFile({ type: "asset", fileName: `pdfjs/${file}`, source: readFileSync(join(PDFJS, file)) });
    },
  };
}

export default defineConfig({
  plugins: [react(), pdfjsAssets()],
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
