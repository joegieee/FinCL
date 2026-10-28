import { expect, type APIRequestContext } from '@playwright/test'
import { readFileSync } from 'node:fs'

// Set FINCL_E2E_PORT to run several checkouts' browser suites at once. serve.py
// reads the same variable.
export const port = Number(process.env.FINCL_E2E_PORT ?? '8799')
export const origin = `http://127.0.0.1:${port}`

/** A synthetic statement PDF that serve.py generated for this run. */
export function statement(name: string): string {
  return `/tmp/fincl-e2e-fixtures-${port}/${name}.pdf`
}

/**
 * Put the server back on the seeded demo ledger, then import `names` through
 * the API. Every spec file calls this from `beforeAll`, so it sees only the
 * statements it names here and its own writes, whichever files ran before it
 * and however they ended.
 */
export async function resetLedger(request: APIRequestContext, ...names: string[]): Promise<void> {
  expect((await request.post('/__e2e__/reset')).status()).toBe(204)
  if (names.length === 0) return
  const { csrf } = await (await request.get('/api/v1/health')).json()
  const headers = { Origin: origin, 'X-CSRF-Token': csrf }
  for (const name of names) {
    const imported = await request.post('/api/v1/imports', {
      headers,
      multipart: {
        adapter: 'REVOLUT_PDF',
        files: { name: `${name}.pdf`, mimeType: 'application/pdf', buffer: readFileSync(statement(name)) },
      },
    })
    expect(imported.ok(), await imported.text()).toBe(true)
    expect((await imported.json()).accepted).toBe(1)
  }
  const refreshed = await request.post('/api/v1/refresh', { headers, data: {} })
  expect(refreshed.ok(), await refreshed.text()).toBe(true)
}
