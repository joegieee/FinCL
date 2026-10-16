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
