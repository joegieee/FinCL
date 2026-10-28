import { expect, test } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { resetLedger } from './ledger'
import { openTab } from './rail'

const fixture = (name: string) => fileURLToPath(new URL(`../../tests/fixtures/${name}`, import.meta.url))

test.beforeAll(async ({ request }) => {
  await resetLedger(request)
})

test('statements from every bank upload together, recognised automatically, and refresh the ledger', async ({ page }) => {
  await page.goto('/?start=2026-01-01&end=2026-12-31')
  await openTab(page, 'Setup')
  // No source chosen: each file's bank is recognised from the file itself.
  await expect(page.getByLabel('Statement source')).toHaveValue('AUTO')
  await page.getByLabel('Statement files').setInputFiles([
    'dbs_multicurrency.pdf', 'paylah_legacy.pdf', 'maribank_savings.pdf',
    'mari_card.pdf', 'trust_savings.pdf', 'revolut_statement.pdf',
  ].map(fixture))
  const uploaded = page.waitForResponse(response => response.url().endsWith('/api/v1/imports') && response.request().method() === 'POST')
  const refreshed = page.waitForResponse(response => response.url().endsWith('/api/v1/refresh') && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Upload statements' }).click()
  const upload = await uploaded
  expect(upload.ok()).toBeTruthy()
  const body = await upload.json()
  expect(body.accepted).toBe(6)
  expect(body.results.map((r: { adapter: string }) => r.adapter)).toEqual([
    'DBS_CONSOLIDATED', 'DBS_PAYLAH', 'MARI_SAVINGS', 'MARI_CARD', 'TRUST_SAVINGS', 'REVOLUT_PDF',
  ])
  expect((await refreshed).ok()).toBeTruthy()
  await expect(page.getByText('6 of 6 files accepted.')).toBeVisible()
  await expect(page.getByText('recognised as Mari Credit Card — PDF statement')).toBeVisible()
  await page.reload()
  await expect(page.locator('.import-table tbody tr').first()).toBeVisible()
  await openTab(page, 'Transactions')
  await page.getByLabel('Filter account').selectOption({ label: 'DBS My Account' })
  await expect(page.locator('.transactions-table')).toContainText('SYNTHETIC PURCHASE')
})
