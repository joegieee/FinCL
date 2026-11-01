import { expect, test } from '@playwright/test'
import { resetLedger, statement } from './ledger'
import { openTab } from './rail'

test.describe.configure({ mode: 'serial' })

test.beforeAll(async ({ request }) => {
  await resetLedger(request)
})

async function upload(page: import('@playwright/test').Page, fixture: string) {
  await openTab(page, 'Setup')
  await page.getByLabel('Statement files').setInputFiles(statement(fixture.replace(/\.csv$/, '')))
  await page.getByRole('button', { name: 'Upload statements', exact: true }).click()
  await expect(page.locator('.upload-result')).toContainText('1 of 1 file accepted')
  const refreshed = page.waitForResponse(response => response.url().endsWith('/api/v1/refresh'))
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'Refresh', exact: true }).click()
  expect((await refreshed).status()).toBe(200)
}

async function classifyGroup(page: import('@playwright/test').Page, label: string, category: string) {
  const group = page.locator('.candidate-card', { hasText: label })
  await expect(group).toBeVisible()
  await group.click()
  // The group review surface is an inline region on the Categorization page,
  // not a modal dialog, and its own heading composes the group name with the
  // rename and merge buttons beside it. The region's accessible name is the
  // stable handle.
  const dialog = page.getByRole('region', { name: `Review ${label}` })
  await dialog.getByLabel('Category').fill(category)
  await dialog.getByLabel('Category').press('Enter')
  await dialog.getByLabel(/Reason/).fill('Reviewed against the synthetic statement evidence')
  // The panel previews the impact automatically as the form changes, so there
  // is no longer an explicit impact or preview step to click through; the
  // review is committed in one action.
  // The last group in the queue offers "Save"; earlier ones offer "Save & next",
  // since only they have a next.
  await dialog.getByRole('button', { name: /^Save( & next)?$/ }).click()
  await expect(dialog).not.toBeVisible()
}

test('inadequate evidence names what is missing instead of asking onboarding budget questions', async ({ page }) => {
  test.skip(test.info().project.name !== 'desktop')
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible()
  await upload(page, 'inadequate-one-month.csv')
  await openTab(page, 'Baseline')
  await expect(page.getByRole('heading', { name: /2026-02.*1 month/ })).toBeVisible()
  await expect(page.getByText('Limited history').first()).toBeVisible()
  await expect(page.getByText(/evidence task/)).toBeVisible()
  await expect(page.getByText(/observed-only|statement period|categor/i).first()).toBeVisible()
  await expect(page.getByText(/enter.*budget|monthly budget questionnaire/i)).toHaveCount(0)
})

test('whole-framework journey derives amounts from evidence and survives backup restore', async ({ page }) => {
  test.setTimeout(90_000)
  test.skip(test.info().project.name !== 'desktop')
  await page.goto('/')

  // Import reconciled history, then inspect the still-visible evidence gaps.
  await upload(page, 'reconciled-history.csv')
  await openTab(page, 'Baseline')
  await expect(page.getByText(/observed-only|categor/i).first()).toBeVisible()
  await expect(page.getByText(/reconciliation \d+\/\d+/)).toBeVisible()

  // Review grouped evidence and create exact reusable rules. No financial
  // amount is entered: the statement supplies salary, cash and living costs.
  await openTab(page, 'Categorization')
  await classifyGroup(page, 'APARTMENT RENT', 'Housing / Rent')
  await classifyGroup(page, 'CAFE SOCIAL', 'Food & Dining / Dining Out')

  await openTab(page, 'Baseline')
  await expect(page.getByText('WHAT YOUR NORMAL LIFE COSTS · SGD')).toBeVisible()
  await expect(page.getByText('LIKELY INCOME STREAMS')).toBeVisible()
  await expect(page.getByText('ACME PAYROLL')).toBeVisible()
  // The reserve basis now names the month count from the chosen policy, and
  // says whether it is the default or a deliberate choice. Reviewing the
  // default is not choosing one, so the two must read differently.
  await expect(page.getByText(/default 3 months of mean essential owner cost/i)).toBeVisible()

  await openTab(page, 'Cash')
  await expect(page.getByText(/Last common supported balance/)).toBeVisible()
  await expect(page.getByText(/account bridge/)).toBeVisible()
  await openTab(page, 'Money Owed')
  await expect(page.getByText('Annual insurance invoice')).toBeVisible()

  // Choose the evidence-derived reserve policy by reviewing the conservative
  // controls that apply it; no reserve amount is entered.
  const controlsRead = page.waitForResponse(response => response.url().includes('/reporting/workspaces/spending-controls?'))
  await openTab(page, 'Controls')
  const controlsResponse = await controlsRead
  expect(controlsResponse.url()).toContain('include_forecast=false')
  expect((await controlsResponse.json()).native).not.toHaveProperty('forecast_cases')
  await expect(page.getByText(/Minimum conservative unallocated, reserve-adjusted balance/)).toBeVisible()

  await openTab(page, 'Annual funds')
  await expect(page.getByText('Annual insurance invoice')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm sourced fund' }).click()
  await expect(page.getByText(/Funding shortfall/)).toBeVisible()

  // Try a source-backed cut using the offered observed amount, not a typed cost.
  await openTab(page, 'Controls')
  const category = page.getByLabel('Source-backed category')
  await category.selectOption({ index: 1 })
  await page.getByRole('button', { name: 'Use full observed amount' }).click()
  const scenarioRead = page.waitForResponse(response => response.url().includes('/spending-controls/what-if?'))
  await page.getByRole('button', { name: 'Compare before and after' }).click()
  const scenarioResponse = await scenarioRead
  expect(scenarioResponse.url()).toContain('include_forecast=false')
  expect((await scenarioResponse.json()).native.scenario).not.toHaveProperty('cases')
  await expect(page.locator('.scenario-result')).toContainText('Hypothetical result only')

  await openTab(page, 'Variance')
  await page.getByRole('button', { name: 'Save current forecast snapshot' }).click()
  await expect(page.getByLabel('Saved snapshot')).not.toHaveValue('')

  await upload(page, 'later-actuals.csv')
  await openTab(page, 'Variance')
  await page.getByLabel('Actuals through').fill('2026-09-30')
  await page.getByRole('button', { name: 'Compare' }).click()
  await expect(page.getByText('Snapshot digest verified')).toBeVisible()
  await expect(page.getByText(/later cash movement/)).toBeVisible()

  await openTab(page, 'Net worth')
  await expect(page.getByText(/tracked net worth/).first()).toBeVisible()

  await openTab(page, 'Maintenance')
  await page.getByRole('button', { name: 'Create verified backup' }).click()
  await expect(page.getByText(/Backup and restore verified|backup-.*evidence object/).first()).toBeVisible()
  await page.getByRole('button', { name: 'Restore into verification copy' }).click()
  await expect(page.getByText('Backup and restore verified')).toBeVisible()
  await expect(page.getByText(/restored generation/)).toBeVisible()
  const refreshed = page.waitForResponse(response => response.url().endsWith('/api/v1/refresh'))
  await page.getByRole('navigation', { name: 'Main navigation' }).getByRole('button', { name: 'Refresh', exact: true }).click()
  expect((await refreshed).status()).toBe(200)
  await page.reload()
  await expect(page.getByText(/New ledger inputs are available/)).toHaveCount(0)
})
