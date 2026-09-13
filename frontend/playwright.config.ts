import { defineConfig, devices } from '@playwright/test'

// The app is served by FastAPI from the built static assets, so `npm run build`
// must run before `npm run test:browser`. serve.py builds an isolated synthetic
// ledger; no real financial data is involved.
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: 'http://127.0.0.1:8799',
    trace: 'on-first-retry',
  },
  projects: [
    { name: 'setup', testMatch: /auth\.setup\.ts/ },
    { name: 'desktop', dependencies: ['setup'],
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 }, storageState: 'e2e/.auth.json' } },
    { name: 'narrow', dependencies: ['setup'],
      use: { ...devices['Desktop Chrome'], viewport: { width: 390, height: 844 }, storageState: 'e2e/.auth.json' } },
  ],
  webServer: {
    command: 'UV_CACHE_DIR=/tmp/fincl-e2e-uv-cache uv run --project .. python e2e/serve.py',
    url: 'http://127.0.0.1:8799/',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
