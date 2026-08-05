import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/rendered-copy",
  fullyParallel: false,
  workers: 1,
  timeout: 180_000,
  expect: { timeout: 5_000 },
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:4431",
    colorScheme: "light",
    serviceWorkers: "block",
    viewport: { width: 1280, height: 720 },
  },
  webServer: {
    command: "./node_modules/.bin/vite preview --host 127.0.0.1 --port 4431 --strictPort",
    url: "http://127.0.0.1:4431",
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
