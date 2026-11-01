import { expect, test } from '@playwright/test'
import { resetLedger } from './ledger'
import { openTab } from './rail'

test.beforeAll(async ({ request }) => {
  await resetLedger(request, 'reconciled-history')
})

// Normal serving opens directly without a launch key.
test.beforeEach(async ({ page }) => {
  test.skip(test.info().project.name !== 'desktop')
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()
})

test('overview shows an exact, currency-qualified spending total', async ({ page }) => {
  const spending = page.getByText(/^Monthly spending/).locator('..')
  // Reported surfaces print the amount before its currency ("2,000.00 SGD");
  // the legacy native `money()` helper prints it the other way round. This card
  // migrated to the reported component, so it is the first form here. What the
  // test is asserting either way is that the figure is exact and carries its
  // currency -- never a bare, unqualified number.
  await expect(spending).toContainText(/[\d,]+\.\d{2} SGD/)
})

test('a category edit survives preview, save and a full page reload', async ({ page }) => {
  await openTab(page, 'Transactions')
  const firstChip = page.locator('.transactions-table .category-chip').first()
  const original = (await firstChip.textContent())?.trim()
  await firstChip.click()

  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  // Category is a searchable combobox now, not a <select>, so there are no
  // options to enumerate: the category is typed and committed.
  const target = original === 'Rent' ? 'Food & Dining / Dining Out' : 'Housing / Rent'
  const category = dialog.getByLabel('Category')
  await category.fill(target)
  await category.press('Enter')
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
