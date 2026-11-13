import http from "node:http";
import path from "node:path";
import { defineConfig, searchForWorkspaceRoot } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const DOCS = path.resolve(__dirname, "../../docs");

// The SPA and API share one origin: in development Vite proxies /api to Django.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // `@docs`: the repository's docs/ folder, which the permissions reference page bundles PERMISSIONS.md from. In the
  // Docker web stage (WORKDIR /src) it resolves to /docs, where the Dockerfile copies that one file.
  resolve: { alias: { "@": path.resolve(__dirname, "src"), "@docs": DOCS } },
  // Playwright runs a second dev server for the first-run setup spec; each keeps its own pre-bundled dependencies.
  cacheDir: process.env.SOFTWARDEN_VITE_CACHE ?? "node_modules/.vite",
  // Pre-bundle the lazily loaded command palette's and API reference's dependencies at start-up: discovered on first
  // use instead, Vite re-optimises and reloads the page, which loses whatever the user (or a Playwright test) was doing.
  optimizeDeps: { include: ["cmdk", "@scalar/api-reference-react"] },
  server: {
    port: Number(process.env.SOFTWARDEN_WEB_PORT ?? 5173),
    strictPort: true,
    // The permissions reference page bundles docs/PERMISSIONS.md (outside this app's folder) as text.
    fs: { allow: [searchForWorkspaceRoot(process.cwd()), DOCS] },
    // A keep-alive agent: without one the proxy opens a new connection to Django for every request (and Django a new
    // database connection with it), which under the Playwright suite exhausted Windows' ephemeral ports.
    // /scim is the SCIM 2.0 service of the optional `ee` package, outside /api/v1/: the Tenant URL is {SOFTWARDEN_BASE_URL}/scim/v2/<slug>,
    // so in development it is served through this origin as well.
    proxy: {
      "/api": { target: process.env.SOFTWARDEN_API ?? "http://127.0.0.1:8010", changeOrigin: false, agent: new http.Agent({ keepAlive: true }) },
      "/scim": { target: process.env.SOFTWARDEN_API ?? "http://127.0.0.1:8010", changeOrigin: false, agent: new http.Agent({ keepAlive: true }) },
    },
  },
});
