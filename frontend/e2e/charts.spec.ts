import { expect, test } from '@playwright/test'
import { openTab } from './rail'

// Synthetic responses isolate chart behavior from the shared ledger's state.
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
  await expect(frame.getByRole('status')).toContainText('Spending fell by SGD 50.00 (25.0%)')
  await expect(frame.getByRole('img').locator('.recharts-bar-rectangle').first()).toBeVisible()
  await frame.getByLabel('Spending chart view').selectOption('change')
  // Zero change lies on the baseline and has no painted rectangle.
  await expect(frame.getByRole('img', { name: 'Month-on-month spending change chart · SGD', exact: true }).locator('.recharts-bar-rectangle')).toHaveCount(3)
  await frame.getByText('Exact values and coverage').click()
  const march = frame.getByRole('row').filter({ hasText: 'Mar 2026' })
  await expect(march).toContainText('Unavailable')
  await expect(march).toContainText('Both months need full coverage.')
  await expect(frame.getByRole('row').filter({ hasText: 'Jun 2026' })).toContainText('-25.0%')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: test.info().outputPath('monthly-spending.png'), fullPage: true })
})
