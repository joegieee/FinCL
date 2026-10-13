import { expect, test } from '@playwright/test'
import { fileURLToPath } from 'node:url'
import { openTab } from './rail'

test('statement sources upload through the UI and refresh the ledger automatically', async ({ page }) => {
  await page.goto('/?start=2026-01-01&end=2026-12-31')
  await openTab(page, 'Setup')
  for (const [adapter, fixture] of [
    ['DBS_CONSOLIDATED', 'dbs_multicurrency.pdf'],
    ['DBS_PAYLAH', 'paylah_legacy.pdf'],
    ['MARI_SAVINGS', 'maribank_savings.pdf'],
    ['MARI_CARD', 'mari_card.pdf'],
    ['TRUST_SAVINGS', 'trust_savings.pdf'],
    ['REVOLUT_PDF', 'revolut_statement.pdf'],
  ]) {
    await page.getByLabel('Statement source').selectOption(adapter)
    await page.getByLabel('Statement files').setInputFiles(fileURLToPath(new URL(`../../tests/fixtures/${fixture}`, import.meta.url)))
    const uploaded = page.waitForResponse(response => response.url().endsWith('/api/v1/imports') && response.request().method() === 'POST')
    const refreshed = page.waitForResponse(response => response.url().endsWith('/api/v1/refresh') && response.request().method() === 'POST')
    await page.getByRole('button', { name: 'Upload statements' }).click()
    const upload = await uploaded
    expect(upload.ok()).toBeTruthy()
    expect((await upload.json()).accepted).toBe(1)
    expect((await refreshed).ok()).toBeTruthy()
    await expect(page.getByText('1 of 1 file accepted.')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Dismiss', exact: true })).toBeEnabled()
  }
  await page.reload()
  await expect(page.locator('.import-table tbody tr').first()).toBeVisible()
  await openTab(page, 'Transactions')
  await page.getByLabel('Filter account').selectOption({ label: 'DBS My Account' })
  await expect(page.locator('.transactions-table')).toContainText('SYNTHETIC PURCHASE')
})
