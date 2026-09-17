import { expect, test, type Page } from '@playwright/test'
import { openTab } from './rail'

test.describe.configure({ mode: 'serial' })

async function upload(page: Page, ...names: string[]) {
  await openTab(page, 'Setup')
  await page.getByLabel('Statement files').setInputFiles(names.map(name => `/tmp/fincl-e2e-fixtures/${name}.pdf`))
  await page.getByRole('button', { name: 'Upload statements', exact: true }).click()
  // The result line singularises: "1 of 1 file accepted", "2 of 2 files accepted".
  await expect(page.locator('.upload-result')).toContainText(
    new RegExp(`${names.length} of ${names.length} files? accepted`),
  )
  const refreshed = page.waitForResponse(response => response.url().endsWith('/api/v1/refresh'))
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'Refresh', exact: true }).click()
  expect((await refreshed).status()).toBe(200)
}

async function openTransactions(page: Page, search: string) {
  await page.goto('/?view=Transactions&start=2025-09-01&end=2026-09-13&currency=SGD')
  await expect(page.getByRole('heading', { name: 'Every recorded transaction.' })).toBeVisible()
  const filtered = page.waitForResponse(response => {
    if (!response.url().includes('/api/v1/reporting/transactions?')) return false
    return new URL(response.url()).searchParams.get('search') === search.trim()
  })
  await page.getByLabel('Search transactions').fill(search)
  await filtered
  await expect(page.locator('.transactions-table tbody tr').first()).toBeVisible()
}

async function chooseCategory(dialog: ReturnType<Page['getByRole']>, category: string) {
  const picker = dialog.getByRole('combobox', { name: 'Category' })
  await picker.fill(category)
  await picker.press('Enter')
  await expect(picker).toHaveValue(category)
}

async function reviewAndSaveSingle(page: Page, reason: string) {
  const dialog = page.getByRole('dialog')
  await dialog.getByLabel(/Reason/).fill(reason)
  await dialog.getByRole('button', { name: 'Review changes' }).click()
  await expect(dialog.getByText('Ready to save')).toBeVisible()
  const reloaded = page.waitForResponse(response => response.url().includes('/api/v1/overview?'))
  await dialog.getByRole('button', { name: 'Save changes' }).click()
  await expect(page.getByText(/Saved to your ledger|Saved; totals awaiting refresh/)).toBeVisible()
  await reloaded
}

async function seedGroceryPreset(page: Page) {
  await page.evaluate(() => localStorage.setItem('fincl.classification.presets.v1', JSON.stringify({
    version: 1,
    presets: [{ id: 'e2e-groceries', name: 'Everyday groceries', fields: {
      category: 'Food & Dining / Groceries', cost_behavior: 'variable_routine', need: 'essential',
    } }],
  })))
}

test('desktop: grocery trio, preset, persisted values, focus return and mixed currencies', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop')
  test.setTimeout(90_000)
  await page.goto('/')
  await upload(page, 'classification-history', 'classification-usd')
  await openTransactions(page, 'GROCERY SOLO LONG SYNTHETIC NAME')

  const details = page.getByRole('button', { name: /Details for GROCERY SOLO/ })
  await details.focus()
  await details.press('Enter')
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  const box = await dialog.boundingBox()
  expect(box && box.x + box.width).toBeGreaterThanOrEqual(1278)
  await chooseCategory(dialog, 'Food & Dining / Groceries')
  await dialog.getByLabel('Spending pattern').selectOption('variable_routine')
  await dialog.getByLabel('Spending need').selectOption('essential')

  let batchRequests = 0
  page.on('request', request => { if (request.url().endsWith('/api/v1/batch')) batchRequests += 1 })
  await dialog.getByLabel('New preset name').fill('Everyday groceries')
  await dialog.getByRole('button', { name: /Save as preset/ }).click()
  expect(batchRequests).toBe(0)
  await reviewAndSaveSingle(page, 'Synthetic grocery trio acceptance')
  expect(batchRequests).toBe(1)

  await openTransactions(page, 'GROCERY SOLO LONG SYNTHETIC NAME')
  const reopened = page.getByRole('button', { name: /Details for GROCERY SOLO/ })
  await reopened.click()
  const reopenedDialog = page.getByRole('dialog')
  await expect(reopenedDialog.getByRole('combobox', { name: 'Category' })).toHaveValue('Food & Dining / Groceries')
  await expect(reopenedDialog.getByRole('combobox', { name: 'Spending pattern' })).toHaveValue('variable_routine')
  await expect(reopenedDialog.getByRole('combobox', { name: 'Spending need' })).toHaveValue('essential')
  await reopenedDialog.press('Escape')
  await expect(reopened).toBeFocused()

  await page.getByLabel('Search transactions').fill('')
  await page.getByLabel('Account currency filter').selectOption('USD')
  await expect(page.getByText('USD SYNTHETIC PURCHASE', { exact: true }).first()).toBeVisible()
  await page.getByLabel('Account currency filter').selectOption('SGD')
  await page.getByLabel('Search transactions').fill('GROCERY SOLO LONG SYNTHETIC NAME')
  await expect(page.getByText('GROCERY SOLO LONG SYNTHETIC NAME', { exact: true }).first()).toBeVisible()
})

test('desktop: selection across pages, preset-only draft, independent exception and fill-missing counts', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop')
  test.setTimeout(120_000)
  await openTransactions(page, 'BULK GROCERY')
  await seedGroceryPreset(page)
  await expect(page.getByText('55 transactions')).toBeVisible()
  const firstPageChecks = page.locator('.transactions-table tbody input[type=checkbox]')
  await firstPageChecks.nth(0).check()
  await firstPageChecks.nth(1).check()
  const firstSelected = await page.locator('.transactions-table tbody tr').nth(0).locator('td[data-label="Merchant"] strong').innerText()
  await page.getByRole('button', { name: /Next/ }).click()
  await expect(page.getByText(/page 2/)).toBeVisible()
  await page.locator('.transactions-table tbody input[type=checkbox]').first().check()
  const secondPageSelected = await page.locator('.transactions-table tbody tr').first().locator('td[data-label="Merchant"] strong').innerText()

  const bulk = page.getByRole('region', { name: 'Bulk assignment' })
  let saves = 0
  page.on('request', request => { if (request.url().endsWith('/api/v1/batch')) saves += 1 })
  await bulk.getByRole('button', { name: 'Everyday groceries', exact: true }).click()
  await expect(bulk.getByLabel('Set spending pattern')).toHaveValue('variable_routine')
  expect(saves).toBe(0)
  await bulk.getByLabel(/Reason/).fill('Synthetic cross-page bulk grocery acceptance')
  await bulk.getByRole('button', { name: 'Review changes' }).click()
  await expect(bulk.getByText(/Ready to save 3 transactions/)).toBeVisible()
  await bulk.getByRole('button', { name: /Save changes for 3 transactions/ }).click()
  await expect(page.getByText(/Saved to your ledger|Saved; totals awaiting refresh/)).toBeVisible()
  expect(saves).toBe(1)

  await openTransactions(page, firstSelected)
  await page.getByRole('button', { name: new RegExp(`Details for ${firstSelected}`) }).click()
  const exception = page.getByRole('dialog')
  await expect(exception.getByRole('combobox', { name: 'Category' })).toHaveValue('Food & Dining / Groceries')
  await exception.getByLabel('Spending pattern').selectOption('irregular')
  await exception.getByLabel('Spending need').selectOption('discretionary')
  await reviewAndSaveSingle(page, 'Synthetic independent grocery exception')
  await openTransactions(page, secondPageSelected)
  await page.getByRole('button', { name: new RegExp(`Details for ${secondPageSelected}`) }).click()
  await expect(page.getByRole('dialog').getByRole('combobox', { name: 'Spending pattern' })).toHaveValue('variable_routine')
  await expect(page.getByRole('dialog').getByRole('combobox', { name: 'Spending need' })).toHaveValue('essential')
  await page.getByRole('dialog').press('Escape')

  // FILL COMPLETE is the control for the bulk count at the end of this test: it
  // already carries a need, so a bulk need assignment must leave it alone. The
  // fixture seeds that state, so this asserts the precondition rather than
  // re-applying it -- re-selecting identical values is not an edit, and the
  // editor correctly refuses to submit one.
  await openTransactions(page, 'FILL COMPLETE')
  await page.getByRole('button', { name: /Details for FILL COMPLETE/ }).click()
  let fillDialog = page.getByRole('dialog')
  await expect(fillDialog.getByRole('combobox', { name: 'Category' })).toHaveValue('Food & Dining / Groceries')
  await expect(fillDialog.getByRole('combobox', { name: 'Spending pattern' })).toHaveValue('variable_routine')
  await expect(fillDialog.getByRole('combobox', { name: 'Spending need' })).toHaveValue('essential')
  await fillDialog.press('Escape')

  await openTab(page, 'Categories')
  await page.getByRole('button', { name: 'New category' }).click()
  const categoryDialog = page.getByRole('dialog')
  await categoryDialog.getByLabel('Category name').fill('Needs review')
  await categoryDialog.getByLabel('Group').fill('Synthetic')
  await categoryDialog.getByLabel('Reason').fill('Synthetic category with no spending-role defaults')
  await categoryDialog.getByRole('button', { name: 'Preview change' }).click()
  const categoryReloaded = page.waitForResponse(response => response.url().includes('/api/v1/overview?'))
  await categoryDialog.getByRole('button', { name: 'Save to ledger' }).click()
  await categoryReloaded
  await openTransactions(page, 'FILL MISSING')
  await page.getByRole('button', { name: /Details for FILL MISSING/ }).click()
  fillDialog = page.getByRole('dialog')
  await chooseCategory(fillDialog, 'Synthetic / Needs review')
  await fillDialog.getByLabel('Spending pattern').selectOption('variable_routine')
  await reviewAndSaveSingle(page, 'Synthetic unresolved need control')

  await openTransactions(page, 'FILL ')
  const fillRows = page.locator('.transactions-table tbody input[type=checkbox]')
  await fillRows.nth(0).check()
  await fillRows.nth(1).check()
  const fillBulk = page.getByRole('region', { name: 'Bulk assignment' })
  await fillBulk.getByLabel('Set spending need').selectOption('discretionary')
  await fillBulk.getByLabel(/Reason/).fill('Synthetic fill-missing acceptance')
  await fillBulk.getByRole('button', { name: 'Review changes' }).click()
  await expect(fillBulk.getByText(/Spending need applies to 2 of 2 selected, 1 changing, 1 already set and left unchanged/)).toBeVisible()
  await fillBulk.getByRole('button', { name: /Save changes for 1 transaction/ }).click()
})

test('desktop: isolated income, recurring payer suggestion, grouped save-next and rule impact', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop')
  test.setTimeout(120_000)
  await openTransactions(page, 'ONE OFF CONSULTING RECEIPT')
  await page.getByRole('button', { name: /Details for ONE OFF/ }).click()
  let dialog = page.getByRole('dialog')
  await chooseCategory(dialog, 'Income & Inflow / Earned Income')
  await dialog.getByLabel('Incoming type').selectOption('income')
  await reviewAndSaveSingle(page, 'Synthetic isolated receipt acceptance')

  await openTab(page, 'Income')
  const payer = page.locator('.income-candidate', { hasText: 'FREELANCE PAYER' })
  await expect(payer).toBeVisible()
  await chooseCategory(payer, 'Income & Inflow / Earned Income')
  await payer.getByLabel('Classification', { exact: true }).selectOption('income')
  await payer.getByLabel('Reason').fill('Synthetic recurring payer acceptance')
  await payer.getByLabel(/Also save a reusable rule/).check()
  await payer.getByRole('button', { name: 'Review changes' }).click()
  await expect(payer.getByTestId('rule-impact')).toContainText('only suggests')
  await payer.getByRole('button', { name: /Save 3 receipts \+ rule/ }).click()

  await openTab(page, 'Categorization')
  // The inbox's old single "focus" selector is gone; its filters are now
  // kind/direction/lifecycle/account/currency. Direction is what this test
  // actually needed: it asserts a spending pattern and an essential need, which
  // only an outgoing group has. Without narrowing, the queue can hand back an
  // inflow group whose panel offers an incoming type instead.
  await page.getByRole('button', { name: /^Filters/ }).click()
  // The queue refetches behind the filter, and the chip appears as soon as the
  // control changes -- before the new list arrives. Reading the first card in
  // that gap captures a row that is about to be replaced, and the panel it
  // names is then never opened. Wait for the refetch itself.
  const refiltered = page.waitForResponse(response =>
    response.url().includes('/api/v1/categorization/candidates?')
    && new URL(response.url()).searchParams.get('direction') === 'OUT')
  await page.getByLabel('Filter categorization direction').selectOption('OUT')
  await refiltered
  // The response landing is not the list re-rendering, and a locator resolves
  // afresh on every use: reading the first card's name from the old list and
  // then clicking a card from the new one opens a panel the name does not
  // match. Waiting for the inflow payer to leave the queue -- the one thing
  // this filter exists to do -- closes that gap.
  await expect(page.locator('.candidate-card', { hasText: 'FREELANCE PAYER' })).toHaveCount(0)
  const group = page.locator('.candidate-card').filter({ hasNotText: 'RULE SHOP COMMON' }).first()
  await expect(group).toBeVisible()
  const groupName = (await group.locator('strong').innerText()).trim()
  await group.click()
  // The group review surface is an inline region, not a modal dialog, and it
  // previews continuously instead of behind an explicit review step.
  const review = page.getByRole('region', { name: `Review ${groupName}` })
  await expect(review).toBeVisible()
  await review.getByRole('radio', { name: /^Selected/ }).check()
  await chooseCategory(review, 'Food & Dining / Groceries')
  await review.getByLabel('Set spending pattern').selectOption('variable_routine')
  await review.getByLabel('Set spending need').selectOption('essential')
  await review.getByLabel(/Reason/).fill('Synthetic grouped queue acceptance')
  // Only the last group in the queue offers a bare "Save"; earlier ones advance.
  const saveNext = review.getByRole('button', { name: /^Save( & next)?$/ })
  const advances = (await saveNext.textContent())?.includes('next') ?? false
  await saveNext.click()
  if (advances) {
    await expect(review).toHaveCount(0)
  }

  await openTransactions(page, 'RULE SHOP COMMON')
  await page.getByRole('button', { name: /Details for RULE SHOP COMMON/ }).first().click()
  dialog = page.getByRole('dialog')
  await chooseCategory(dialog, 'Food & Dining / Dining Out')
  await dialog.getByLabel('Spending pattern').selectOption('irregular')
  await dialog.getByLabel('Spending need').selectOption('discretionary')
  await reviewAndSaveSingle(page, 'Synthetic protected manual exception')
  await openTransactions(page, 'RULE SHOP COMMON')
  await page.getByRole('button', { name: /Details for RULE SHOP COMMON/ }).nth(1).click()
  dialog = page.getByRole('dialog')
  await chooseCategory(dialog, 'Food & Dining / Groceries')
  await dialog.getByLabel('Spending pattern').selectOption('variable_routine')
  await dialog.getByLabel('Spending need').selectOption('essential')
  await dialog.getByLabel(/Reason/).fill('Synthetic reusable rule acceptance')
  await dialog.getByRole('button', { name: /Also save a reusable rule/ }).click()
  await dialog.getByRole('button', { name: 'Check for a reusable predicate' }).click()
  await dialog.getByRole('radio').first().check()
  await dialog.getByLabel(/Reusable category rule/).check()
  await dialog.getByLabel(/Reusable spending rule/).check()
  await dialog.getByRole('button', { name: 'Preview rule' }).click()
  await expect(dialog.getByText('Category rule impact')).toBeVisible()
  await expect(dialog.getByText('Spending-role rule impact')).toBeVisible()
  // The panel states the protection in both its predicate summary and its
  // impact line now, so either one satisfies this.
  await expect(dialog.getByText(/manual override.*protected/).first()).toBeVisible()
  await dialog.getByRole('button', { name: 'Save rule' }).click()

  await upload(page, 'classification-future')
  await openTransactions(page, 'FREELANCE PAYER')
  await expect(page.getByText('4 transactions')).toBeVisible()
  const futureRow = page.locator('.transactions-table tbody tr').filter({ hasText: '6 Jan 2026' })
  await expect(futureRow).toContainText('Needs review')
})

test('desktop: failed preview, uncertain retry and refresh-pending acknowledgement', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop')
  test.setTimeout(90_000)
  await openTransactions(page, 'BULK GROCERY 53')
  await page.getByRole('button', { name: /Details for BULK GROCERY 53/ }).click()
  let dialog = page.getByRole('dialog')
  // This test is about a failed preview and an idempotent retry; the values are
  // incidental, but they must actually differ from what earlier tests in this
  // file left on this transaction. Re-selecting identical values is not an edit
  // and the editor correctly keeps the submit disabled, so a no-op edit would
  // stall here rather than exercise the retry.
  await chooseCategory(dialog, 'Food & Dining / Groceries')
  await dialog.getByLabel('Spending pattern').selectOption('irregular')
  await dialog.getByLabel('Spending need').selectOption('discretionary')
  await dialog.getByLabel(/Reason/).fill('Synthetic uncertain retry acceptance')

  let previewFailed = false
  await page.route('**/api/v1/classification/preview', async route => {
    if (!previewFailed) {
      previewFailed = true
      await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: 'Synthetic preview outage' }) })
    } else await route.continue()
  })
  await dialog.getByRole('button', { name: 'Review changes' }).click()
  await expect(dialog.getByRole('alert')).toContainText('Synthetic preview outage')
  await dialog.getByLabel(/Reason/).fill('Synthetic uncertain retry acceptance after preview recovery')
  await dialog.getByRole('button', { name: 'Review changes' }).click()
  await expect(dialog.getByText('Ready to save')).toBeVisible()
  await page.unroute('**/api/v1/classification/preview')

  const ids: string[] = []
  let aborted = false
  await page.route('**/api/v1/batch', async route => {
    ids.push((route.request().postDataJSON() as { request_id: string }).request_id)
    if (!aborted) { aborted = true; await route.abort('connectionreset') }
    else await route.continue()
  })
  await dialog.getByRole('button', { name: 'Save changes' }).click()
  await expect(dialog.getByText(/outcome may be unknown/)).toBeVisible()
  await dialog.getByRole('button', { name: 'Retry same save' }).click()
  await expect(page.getByText(/Saved to your ledger/)).toBeVisible()
  expect(ids).toHaveLength(2)
  expect(new Set(ids).size).toBe(1)
  await page.unroute('**/api/v1/batch')

  await openTransactions(page, 'BULK GROCERY 54')
  await page.getByRole('button', { name: /Details for BULK GROCERY 54/ }).click()
  dialog = page.getByRole('dialog')
  await chooseCategory(dialog, 'Food & Dining / Groceries')
  await dialog.getByLabel('Spending pattern').selectOption('variable_routine')
  await dialog.getByLabel('Spending need').selectOption('essential')
  await dialog.getByLabel(/Reason/).fill('Synthetic refresh pending acceptance')
  await dialog.getByRole('button', { name: 'Review changes' }).click()
  let pendingSaves = 0
  await page.route('**/api/v1/batch', async route => {
    pendingSaves += 1
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'recorded_refresh_pending' }) })
  })
  await dialog.getByRole('button', { name: 'Save changes' }).click()
  await expect(page.getByText('Saved; totals awaiting refresh. Use Refresh to retry.')).toBeVisible()
  expect(pendingSaves).toBe(1)
})

test('narrow: individual and bulk flows are keyboard reachable with a persistent action footer', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'narrow')
  test.setTimeout(90_000)
  await openTransactions(page, 'BULK GROCERY 55')
  await seedGroceryPreset(page)
  const row = page.locator('.transactions-table tbody tr').first()
  await row.focus()
  await row.press(' ')
  await row.press('e')
  const dialog = page.getByRole('dialog')
  await expect(dialog).toBeVisible()
  const box = await dialog.boundingBox()
  expect(box?.width).toBe(390)
  await chooseCategory(dialog, 'Food & Dining / Groceries')
  const pattern = dialog.getByLabel('Spending pattern')
  await pattern.focus(); await pattern.press('ArrowDown'); await pattern.press('ArrowDown'); await pattern.press('Enter')
  const need = dialog.getByLabel('Spending need')
  await need.focus(); await need.press('ArrowDown'); await need.press('Enter')
  await dialog.getByLabel(/Reason/).fill('Synthetic narrow keyboard acceptance')
  await dialog.evaluate(element => { element.scrollTop = element.scrollHeight })
  await expect(dialog.getByRole('button', { name: 'Review changes' })).toBeInViewport()
  await dialog.getByRole('button', { name: 'Review changes' }).press('Enter')
  await dialog.getByRole('button', { name: 'Save changes' }).press('Enter')

  await openTransactions(page, 'BULK GROCERY 51')
  const bulkRow = page.locator('.transactions-table tbody tr').first()
  await bulkRow.focus(); await bulkRow.press(' '); await bulkRow.press('b')
  const bulk = page.getByRole('region', { name: 'Bulk assignment' })
  await expect(bulk).toBeFocused()
  await bulk.getByRole('button', { name: 'Everyday groceries', exact: true }).press('Enter')
  await bulk.getByLabel(/Reason/).fill('Synthetic narrow keyboard bulk acceptance')
  await bulk.getByRole('button', { name: 'Review changes' }).press('Enter')
  await bulk.getByRole('button', { name: /Save changes for 1 transaction/ }).press('Enter')
  await expect(page.getByText(/Saved to your ledger/)).toBeVisible()
})
