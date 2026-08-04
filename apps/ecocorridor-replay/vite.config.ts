import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

function developmentCsp(): Plugin {
  return {
    name: "ecocorridor-development-csp",
    apply: "serve",
    transformIndexHtml(html) {
      // Vite injects development styles and opens a loopback HMR socket. The
      // production artifact retains the fail-closed CSP declared in index.html.
      return html
        .replace("connect-src 'none'", "connect-src 'self' ws://127.0.0.1:1431")
        .replace("style-src 'self'", "style-src 'self' 'unsafe-inline'");
    },
  };
}

export default defineConfig({
  plugins: [developmentCsp(), react()],
  base: "./",
  clearScreen: false,
  // The replay has a single local entry chunk. Disabling Vite's fallback
  // avoids shipping a same-origin `fetch()` path that would conflict with the
  // production `connect-src 'none'` boundary on older browsers.
  build: { modulePreload: { polyfill: false } },
  server: {
    host: "127.0.0.1",
    port: 1431,
    strictPort: true,
    // Fontsource packages live in the worktree-level pnpm store. Keep the
    // development server scoped to this monorepo while allowing those assets.
    fs: { allow: ["../.."] },
  },
});
