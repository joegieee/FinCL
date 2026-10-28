import { expect, test } from '@playwright/test'
import { resetLedger, statement } from './ledger'
import { openTab } from './rail'

// Two end-to-end routines, each completed only through the UI and each ending
// at the question the ledger exists to answer: will there be enough.
//
// - Monthly: import a new statement, go straight from the import to the
//   decisions it created, make them, and read the answer.
// - Commitments: record a debt, find it among everything owed, and see it
//   counted in that same answer.
//
// The ledger starts with the history and later actuals that the late-September
// statement continues without a gap. The classification fixtures stay out: their
// 2025 and USD pockets are too stale to share a cash cutoff, which withholds the
// answer this journey ends on. Both routines assert on what they themselves add
// rather than on totals.
test.describe.configure({ mode: 'serial' })

test.beforeAll(async ({ request }) => {
  await resetLedger(request, 'reconciled-history', 'later-actuals')
})

test.beforeEach(async () => {
  test.skip(test.info().project.name !== 'desktop')
})

test('monthly routine: import, review what it created, and read the answer', async ({ page }) => {
  test.setTimeout(90_000)
  await page.goto('/')
  await openTab(page, 'Setup')
  await page.getByLabel('Statement files').setInputFiles(statement('monthly-late-september'))
  const refreshed = page.waitForResponse(response => response.url().endsWith('/api/v1/refresh') && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Upload statements', exact: true }).click()
  await expect(page.locator('.upload-result')).toContainText('1 of 1 file accepted')

  // An accepted import refreshes the ledger itself, so it is never left stale.
  expect((await refreshed).status()).toBe(200)
  await expect(page.getByText('New ledger inputs are available')).toHaveCount(0)

  // And the import's own result leads to the decisions it created.
  await page.getByRole('button', { name: 'Review new transactions' }).click()
  await expect(page.getByRole('heading', { name: 'Teach it once.' })).toBeVisible()

  // A payer never seen before. Choosing "Income" for all matching inflows
  // saves a rule, which only ever suggests; the inflow on screen must still be
  // classified by the same save, or the group can never leave the inbox.
  const payer = page.locator('.candidate-card', { hasText: 'SIDE GIG PAYER' })
  await payer.click()
  const payerReview = page.getByRole('region', { name: 'Review SIDE GIG PAYER' })
  await payerReview.getByRole('radio', { name: /All matching, now and later/ }).click()
  await payerReview.getByRole('combobox', { name: 'Set incoming type' }).selectOption('income')
  await expect(payerReview.getByText(/Also classifies the inflow shown here as Income now/)).toBeVisible()
  await payerReview.getByRole('button', { name: /^Save( & next)?$/ }).click()
  await expect(page.locator('.candidate-card', { hasText: 'SIDE GIG PAYER' })).toHaveCount(0)

  // A merchant no rule knows yet.
  await page.locator('.candidate-card', { hasText: 'BOOKSHOP SYNTHETIC' }).click()
  const shopReview = page.getByRole('region', { name: 'Review BOOKSHOP SYNTHETIC' })
  const category = shopReview.getByRole('combobox', { name: 'Category' })
  await category.fill('Shopping / Books & Stationery')
  await category.press('Enter')
  await expect(category).toHaveValue('Shopping / Books & Stationery')
  await shopReview.getByRole('button', { name: /^Save( & next)?$/ }).click()
  await expect(page.locator('.candidate-card', { hasText: 'BOOKSHOP SYNTHETIC' })).toHaveCount(0)

  await openTab(page, 'Review')
  await expect(page.getByRole('heading', { name: 'What needs a decision.' })).toBeVisible()

  await openTab(page, 'Will I have enough')
  await expect(page.getByText('WILL I HAVE ENOUGH?', { exact: true })).toBeVisible()
  await expect(page.getByText('Not enough evidence to say')).toHaveCount(0)
})

test('commitments: a recorded debt reaches everything owed and the answer', async ({ page }) => {
  test.setTimeout(60_000)
  await page.goto('/')
  await openTab(page, 'Debts')
  await page.getByRole('button', { name: 'New debt' }).click()
  await page.getByLabel('Direction').selectOption({ label: 'I owe them' })
  await page.getByLabel('Counterparty label').fill('Jordan (e2e loan)')
  await page.getByLabel('Currency').fill('SGD')
  await page.getByLabel('Opened on').fill('2026-09-25')
  await page.getByLabel('Due on').fill('2026-12-15')
  await page.getByLabel('Reason').fill('Borrowed from Jordan for the e2e journey')
  await page.getByRole('button', { name: 'Preview and save' }).click()

  // A new debt has no amount until its first event, so saving it lands on
  // that event rather than back on a list showing it at zero.
  await expect(page.getByRole('heading', { name: 'Record an event · Jordan (e2e loan)' })).toBeVisible()
  await page.getByLabel('Event principal').fill('1500.00')
  await page.getByLabel(/Opening balance/).check()
  await page.getByLabel('Reason').fill('Jordan paid the invoice directly')
  await page.getByRole('button', { name: 'Check event' }).click()
  await page.getByRole('button', { name: 'Save event' }).click()

  const row = page.locator('.debt-list, .debts-view').getByText('Jordan (e2e loan)')
  await expect(row).toBeVisible()
  await expect(page.locator('.debts-view')).toContainText('1,500.00 SGD')

  await openTab(page, 'Obligations')
  await expect(page.getByRole('cell', { name: /Jordan \(e2e loan\)/ })).toBeVisible()

  // Whatever the verdict, the answer names this debt among what it counted.
  await openTab(page, 'Will I have enough')
  await expect(page.getByText(/Jordan \(e2e loan\) · 15 Dec 2026/).first()).toBeVisible()
})
