import { defineConfig, devices } from "@playwright/test";

const snapshotPlatformSuffix = process.platform === "linux" ? "-linux" : "";

export default defineConfig({
  testDir: "./tests",
  // Text rasterization differs materially between macOS and the pinned Linux
  // CI browser. Keep strict pixel comparisons against reviewed baselines for
  // each renderer instead of weakening the global screenshot threshold.
  snapshotPathTemplate: `{testDir}/__screenshots__/{arg}${snapshotPlatformSuffix}{ext}`,
  fullyParallel: true,
  use: {
    baseURL: "http://127.0.0.1:1430",
    colorScheme: "light",
  },
  webServer: [
    {
      command: "pnpm dev",
      url: "http://127.0.0.1:1430",
      reuseExistingServer: !process.env.CI,
    },
    {
      command: "pnpm --dir ../ecocorridor-replay dev",
      url: "http://127.0.0.1:1431",
      reuseExistingServer: !process.env.CI,
    },
  ],
  projects: [
    { name: "desktop-1440", use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } } },
    { name: "desktop-1280", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 720 } } },
    { name: "compact-760", use: { ...devices["Desktop Chrome"], viewport: { width: 760, height: 900 } } },
    { name: "zoom-200", use: { ...devices["Desktop Chrome"], viewport: { width: 720, height: 900 } } },
  ],
});
