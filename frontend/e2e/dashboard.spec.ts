import { expect, test } from '@playwright/test'

// The session cookie is provisioned once by auth.setup.ts and reused here.
test.beforeEach(async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'A clearer picture.' })).toBeVisible()
})

test('overview shows an exact, currency-qualified spending total', async ({ page }) => {
  const spending = page.locator('.metric', { hasText: 'Spending' })
  await expect(spending.locator('strong')).toHaveText(/^SGD [\d,]+\.\d{2}$/)
})

test('a category edit survives preview, save and a full page reload', async ({ page }) => {
  await page.locator('.navigation').getByRole('button', { name: /Transactions/ }).click()
  const firstChip = page.locator('.transactions-table .category-chip').first()
  const original = (await firstChip.textContent())?.trim()
  await firstChip.click()

  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  const select = dialog.locator('form').getByRole('combobox').last()
  const options = await select.locator('option').allTextContents()
  const target = options.find((o) => o.trim() && o.trim() !== original) as string
  await select.selectOption({ label: target })
  await dialog.locator('label', { hasText: 'Reason' }).locator('input').fill('End-to-end reclassification check')

  await dialog.getByRole('button', { name: /Preview change/i }).click()
  await expect(dialog.getByText('Ready to save')).toBeVisible()
  await dialog.getByRole('button', { name: /Save to ledger/i }).click()
  await expect(page.getByText(/Saved to your ledger|totals awaiting refresh/)).toBeVisible()

  await page.reload()
  await page.locator('.navigation').getByRole('button', { name: /Transactions/ }).click()
  await expect(page.locator('.transactions-table .category-chip').first())
    .toHaveText(target.split(' / ').at(-1)!.trim())
})

test('the review tab reflects the ledger state', async ({ page }) => {
  await page.locator('.navigation').getByRole('button', { name: /^Review/ }).click()
  await expect(page.getByRole('heading', { name: /awaiting a decision/ })).toBeVisible()
})
