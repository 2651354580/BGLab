import { defineConfig, type Connect } from "vite";
import { fileURLToPath, URL } from "node:url";
import { readFileSync } from "node:fs";

// The host serves these same shared files in integrated games. Reuse them for
// local hot-seat development and preview without copying a second shell.
const sharedAssets: Connect.NextHandleFunction = (request, response, next) => {
  const name = request.url?.split("?")[0]?.match(/^\/__bglab_shared\/(game-bridge\.js|game-shell\.js|game-shell\.css)$/)?.[1];
  if (!name) return next();
  response.setHeader("Content-Type", name.endsWith(".css") ? "text/css; charset=utf-8" : "text/javascript; charset=utf-8");
  response.end(readFileSync(new URL(`../shared/ui/${name}`, import.meta.url)));
};

export default defineConfig({
  base: "./",
  publicDir: "public",
  plugins: [{
    name: "white-castle-local-shared-ui",
    configureServer(server) { server.middlewares.use(sharedAssets); },
    configurePreviewServer(server) { server.middlewares.use(sharedAssets); },
  }],
  resolve: {
    alias: {
      "node:module": fileURLToPath(
        new URL("./src/adapter/browser-node-module-shim.ts", import.meta.url),
      ),
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
