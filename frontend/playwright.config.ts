import { defineConfig, devices } from '@playwright/test'
import { realpathSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { origin } from './e2e/ledger'

// The app is served by FastAPI from the built static assets, so `npm run build`
// must run before `npm run test:browser`. serve.py builds an isolated synthetic
// ledger; no real financial data is involved. Each spec file resets that ledger
// before it starts (`resetLedger` in e2e/ledger.ts), so files do not depend on
// the order they run in.
const checkout = realpathSync(fileURLToPath(new URL('..', import.meta.url)))

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  // One server, one ledger: a second worker's reset would swap it out from
  // under the first.
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: origin,
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
    // Only a serve.py from this checkout answers this URL, so a server another
    // checkout left on the port is not reused: the run fails on the busy port.
    url: `${origin}/__e2e__/ready?checkout=${encodeURIComponent(checkout)}`,
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
})
