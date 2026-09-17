import { expect, test } from '@playwright/test'
import { openTab } from './rail'

// The session cookie is provisioned once by auth.setup.ts and reused here.
test.beforeEach(async ({ page }) => {
  test.skip(test.info().project.name !== 'desktop')
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'A clearer picture.' })).toBeVisible()
})

test('overview shows an exact, currency-qualified spending total', async ({ page }) => {
  const spending = page.getByText('Average monthly spending + fees').locator('..')
  await expect(spending).toContainText(/SGD [\d,]+\.\d{2}/)
})

test('a category edit survives preview, save and a full page reload', async ({ page }) => {
  await openTab(page, 'Transactions')
  const firstChip = page.locator('.transactions-table .category-chip').first()
  const original = (await firstChip.textContent())?.trim()
  await firstChip.click()

  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  const select = dialog.getByLabel('Category')
  const options = await select.locator('option').allTextContents()
  const target = options.find((o) => o.trim() && o.trim() !== 'Choose a category' && o.trim() !== original) as string
  await select.selectOption({ label: target })
  await dialog.locator('label', { hasText: 'Reason' }).locator('input').fill('End-to-end reclassification check')

  await dialog.getByRole('button', { name: 'Review changes' }).click()
  await expect(dialog.getByText('Ready to save')).toBeVisible()
  await dialog.getByRole('button', { name: 'Save changes' }).click()
  await expect(page.getByText(/Saved to your ledger|recorded.*refresh pending/i)).toBeVisible()

  await page.reload()
  await openTab(page, 'Transactions')
  await expect(page.locator('.transactions-table .category-chip').first())
    .toHaveText(target.split(' / ').at(-1)!.trim())
})

test('the transactions list is served and paged by the backend', async ({ page }) => {
  const request = page.waitForRequest((r) => r.url().includes('/api/v1/reporting/transactions'))
  await openTab(page, 'Transactions')
  const url = new URL((await request).url())
  expect(url.searchParams.get('generation')).toBeTruthy()
  expect(url.searchParams.get('limit')).toBe('50')

  await expect(page.locator('.transactions-table tbody tr').first()).toBeVisible()
  await expect(page.locator('.pagination span')).toHaveText(/Showing \d+ of \d+/)

  // Sorting re-queries the server rather than reordering in the client.
  const resort = page.waitForRequest((r) => r.url().includes('sort=amount'))
  await page.getByLabel('Sort transactions').selectOption('amount')
  await resort
})

test('the review tab reflects the ledger state', async ({ page }) => {
  await openTab(page, 'Review')
  await expect(page.getByRole('heading', { name: /awaiting a decision/ })).toBeVisible()
})
