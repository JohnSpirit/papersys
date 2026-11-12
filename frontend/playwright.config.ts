import { defineConfig } from "@playwright/test";

const serverURL = process.env.PLAYWRIGHT_BASE_URL || "http://127.0.0.1:5173";
const basePath = process.env.PLAYWRIGHT_BASE_PATH || (process.env.PLAYWRIGHT_BASE_URL ? "/papersys/" : "/");
process.env.PLAYWRIGHT_BASE_PATH ||= basePath;
const production = basePath !== "/";
const baseURL = new URL(basePath, serverURL).toString();

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  reporter: "list",
  use: {
    baseURL,
    headless: true,
    viewport: { width: 1440, height: 1000 },
    trace: "retain-on-failure",
  },
  webServer: {
    command: production
      ? `VITE_BASE_PATH=${basePath} npm run build && ./node_modules/.bin/serve -l tcp://127.0.0.1:4173 dist --no-clipboard`
      : "npm run dev -- --host 127.0.0.1",
    url: baseURL,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
