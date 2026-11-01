import { expect, test } from '@playwright/test'
import { resetLedger } from './ledger'
import { openTab } from './rail'

test.beforeAll(async ({ request }) => {
  await resetLedger(request)
})

// Synthetic responses isolate chart behavior from the ledger's state.
test('money charts render, switch metrics and fit narrow and dark screens', async ({ page }) => {
  const report = { report_id: 'chart-demo', name: 'Chart demo', lens: 'recorded_flow', reason: 'Synthetic chart check',
    group_by: ['category'], columns: ['gross_cost', 'refunds', 'net_cost'],
    scope: { window: { kind: 'all' }, currencies: [], accounts: [], categories: [], trips: [], countries: [] } }
  await page.route('**/api/v1/reports', route => route.fulfill({ json: { reports: [report] } }))
  const cell = (value: string) => ({ total: { value, currency: 'SGD' }, partial_total: null, unavailable_reasons: [], excluded_detail: [] })
  const totals = { assets: cell('1200.00'), liabilities: cell('1500.00'), net_worth: cell('-300.00'), manual_assets: cell('0.00'), manual_liabilities: cell('0.00'), net_worth_including_manual: cell('-300.00') }
  await page.route('**/api/v1/reporting/net-worth**', route => route.fulfill({ json: {
    generation: 'demo', reporting_currency: 'SGD', as_of: '2026-10-17', disclaimer: 'Synthetic tracked positions only.',
    totals, components: [], per_currency: {}, notes: [],
    trend: { prior_as_of: '2026-09-17', current_as_of: '2026-10-17', prior_net_worth: cell('100.00'), current_net_worth: cell('-300.00'),
      scope_changed: false, both_complete: true, comparability: 'Same components.', scope_changes: [], delta: { value: '-400.00', currency: 'SGD' } },
  } }))
  await page.route('**/api/v1/reporting/workspaces/reports/chart-demo/result', route => route.fulfill({ json: {
    generation: 'demo', evaluated_on: '2026-10-17', lens: 'recorded_flow', lens_note: 'Synthetic recorded flow.', report,
    window: { start: null, end: null }, data_through: null, group_labels: {}, warnings: [],
    rows: Array.from({ length: 12 }, (_, i) => ({ group: [{ key: 'category', value: `g${i}`, label: `Synthetic category ${i}` }], currency: 'SGD', gross_cost: '100.00', refunds: '120.00', net_cost: '-20.00', entry_count: 1 })),
    totals_by_currency: {},
  } }))
  await page.goto('/')
  await openTab(page, 'Reports')
  await page.getByRole('button', { name: /Chart demo/ }).click()
  const chart = page.getByRole('img', { name: 'Net by report group · SGD', exact: true })
  await expect(chart.locator('.recharts-bar-rectangle').first()).toBeVisible()
  await page.getByLabel('Chart metric').selectOption('refunds')
  await expect(page.getByRole('img', { name: 'Refunds by report group · SGD', exact: true }).locator('.recharts-bar-rectangle')).toHaveCount(12)
  await page.emulateMedia({ colorScheme: 'dark' })
  const scroll = page.getByRole('region', { name: 'Refunds by report group scrollable plot' })
  expect(await scroll.evaluate(el => el.scrollWidth > el.clientWidth)).toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: test.info().outputPath('report-charts.png'), fullPage: true })

  await openTab(page, 'Net worth')
  await expect(page.getByRole('heading', { name: 'Assets, liabilities and the balance' })).toBeVisible()
  await expect(page.getByRole('img', { name: 'How the tracked balance changed · SGD', exact: true }).locator('.recharts-bar-rectangle')).toHaveCount(2)
  const frame = page.locator('.chart-card').filter({ hasText: 'Assets, liabilities and the balance' })
  await frame.getByText('Exact values and coverage').click()
  await expect(frame.getByRole('columnheader', { name: 'Amount (SGD)' })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: test.info().outputPath('networth-charts.png'), fullPage: true })
})

test('monthly spending compares adjacent months and explains increases and decreases', async ({ page }) => {
  const cv = (value: string | null) => ({ total: value === null ? null : { value, currency: 'SGD' }, partial_total: null,
    status: value === null ? 'unavailable' : 'available', unavailable_reasons: [], excluded_detail: [] })
  const trend = [
    { month: '2026-01', amount: '100.00', previous: '80.00', delta: '20.00', pct: '25.0' },
    { month: '2026-02', amount: '150.00', previous: '100.00', delta: '50.00', pct: '50.0' },
    { month: '2026-03', amount: null, previous: '150.00', delta: null, pct: null },
    { month: '2026-04', amount: '200.00', previous: null, delta: null, pct: null },
    { month: '2026-05', amount: '200.00', previous: '200.00', delta: '0.00', pct: '0.0' },
    { month: '2026-06', amount: '150.00', previous: '200.00', delta: '-50.00', pct: '-25.0' },
  ].map(row => ({ month: row.month, spending: cv(row.amount), fees: cv('0.00'), income: cv('0.00'), complete: row.amount !== null,
    previous_spending: cv(row.previous), spending_change: row.delta === null ? null : { value: row.delta, currency: 'SGD' },
    spending_change_pct: row.pct, spending_change_reason: row.delta === null ? 'Both months need full coverage.' : null }))
  await page.route('**/api/v1/reporting/analysis?**', route => route.fulfill({ json: {
    generation: 'demo', reporting_currency: 'SGD', covered: true, comparison_kind: 'none',
    summary: { spending: { current: cv('150.00'), comparison_available: false } },
    trend, category_ranking: [], merchant_ranking: [], contributors_handle: null,
  } }))
  await page.goto('/')
  await openTab(page, 'Analysis')
  const frame = page.locator('.chart-card').filter({ hasText: 'How has my spending changed?' })
  await page.emulateMedia({ colorScheme: 'light' })
  await expect(frame.getByRole('status')).toContainText('Spending fell by 50.00 SGD (25.0%)')
  await expect(frame.getByRole('img').locator('.recharts-bar-rectangle').first()).toBeVisible()
  await expect(frame.getByRole('button', { name: 'Compare months' })).toHaveAttribute('aria-pressed', 'true')
  await frame.screenshot({ path: test.info().outputPath('monthly-comparison-light.png') })
  if (test.info().project.name === 'desktop') {
    await frame.locator('.recharts-bar-rectangle').first().hover()
    await expect(frame.locator('.chart-tooltip')).toContainText('Purchases')
    await expect(frame.locator('.chart-tooltip')).toContainText('100.00 SGD')
    await frame.screenshot({ path: test.info().outputPath('monthly-tooltip.png') })
  }
  await frame.getByRole('button', { name: 'Monthly change' }).click()
  // Zero change lies on the baseline and has no painted rectangle.
  await expect(frame.getByRole('img', { name: 'Month-on-month spending change chart · SGD', exact: true }).locator('.recharts-bar-rectangle')).toHaveCount(3)
  await frame.getByText('Exact values and coverage').click()
  const march = frame.getByRole('row').filter({ hasText: 'Mar 2026' })
  await expect(march).toContainText('Unavailable')
  await expect(march).toContainText('Both months need full coverage.')
  await expect(frame.getByRole('row').filter({ hasText: 'Jun 2026' })).toContainText('-25.0%')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: test.info().outputPath('monthly-spending.png'), fullPage: true })
  await frame.getByText('Exact values and coverage').click()
  await page.emulateMedia({ colorScheme: 'dark' })
  await frame.screenshot({ path: test.info().outputPath('monthly-change-dark.png') })
  const gradientIds = await page.locator('linearGradient').evaluateAll(nodes => nodes.map(node => node.id))
  expect(new Set(gradientIds).size).toBe(gradientIds.length)
})

test('forecast uses distinct case lines and a subtle balance area in both themes', async ({ page }) => {
  const cv = (value: string) => ({ total: { value, currency: 'SGD' }, partial_total: null, unavailable_reasons: [], excluded_detail: [] })
  const valued = (value: string) => ({ native: { value, currency: 'SGD' }, reported: { value, currency: 'SGD' }, purpose: 'forecast', basis: 'identity', valuation_as_of: '2026-01-31', rate_path: [], unavailable: null })
  const months = (values: string[]) => values.map((value, i) => ({ month: `2026-0${i + 2}`, opening: valued('1200.00'), closing: valued(value), income: valued('1000.00'), expense: valued('1100.00'), lowest_balance: valued(value) }))
  const base = { months: months(['1100.00', '950.00', '1050.00', '800.00', '900.00', '750.00']), events: [] }
  const conservative = { months: months(['1000.00', '800.00', '650.00', '500.00', '300.00', '100.00']), events: [] }
  const scenario = { months: [], first_zero: { occurs: false }, first_reserve_breach: { available: false } }
  await page.route('**/api/v1/reporting/forecast', route => route.fulfill({ json: {
    generation: 'demo', reporting_currency: 'SGD', valuation_as_of: '2026-01-31',
    currencies: [{ currency: 'SGD', starting_balance: valued('1200.00'), base, conservative }],
    totals: { base_closing: cv('750.00'), conservative_closing: cv('100.00'), base_combined_minimum: { amount: cv('750.00'), month: '2026-07' }, conservative_combined_minimum: { amount: cv('100.00'), month: '2026-07' } },
    native: { available: true, limitations: [], horizon: { supported_cash_cutoff: '2026-01-31', end: '2026-07-31' },
      by_currency: { SGD: { available: true, starting_balance: '1200.00', starting_balance_as_of: '2026-01-31', base: scenario, conservative: scenario } } },
  } }))
  await page.goto('/')
  await openTab(page, 'Forecast')
  const chart = page.locator('.chart-card').filter({ hasText: 'Balance by month' })
  await expect(chart.locator('.recharts-line-curve')).toHaveCount(2)
  await expect(chart.locator('.recharts-area-area')).toBeVisible()
  for (const colorScheme of ['light', 'dark'] as const) {
    await page.emulateMedia({ colorScheme })
    await chart.screenshot({ path: test.info().outputPath(`forecast-${colorScheme}.png`) })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  }
})
