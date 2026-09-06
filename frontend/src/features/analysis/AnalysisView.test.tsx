import type React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('recharts')>()
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) =>
      <div style={{ width: 600, height: 300 }}>{children}</div>,
  }
})

const money = (v: string) => ({ value: v, currency: 'SGD' })

function analysis(over: Record<string, unknown> = {}) {
  return {
    generation: 'sha256:abc', freshness: 'current', updated_at: '2026-03-01T00:00:00+00:00',
    scope: { start: '2026-02-01', end: '2026-02-28', currency: 'SGD', lens: 'recorded_flow', accounts: [], category_keys: [], merchant: null },
    covered: true, coverage_reasons: [],
    comparison: { kind: 'previous_month', start: '2026-01-01', end: '2026-01-31', available: true, reasons: [] },
    summary: {
      spending: { value: money('27.50'), previous: money('50.00'), delta: money('-22.50'), pct: '-45.0' },
      fees: { value: money('0'), previous: money('0.50'), delta: money('-0.50'), pct: '-100.0' },
      refunds: { value: money('5.00'), previous: money('0'), delta: money('5.00'), pct: null },
      income: { value: money('0'), previous: money('0'), delta: money('0'), pct: null },
      reimbursements: { value: money('0'), previous: money('0'), delta: money('0'), pct: null },
      non_income_inflow: { value: money('0'), previous: money('0'), delta: money('0'), pct: null },
      pending_review: { value: money('0'), previous: money('900.00'), delta: money('-900.00'), pct: '-100.0' },
      net_economic_flow: { value: money('-22.50'), previous: money('-50.50'), delta: money('28.00'), pct: '-55.4' },
    },
    summary_handles: { spending: 'h-spend', refunds: 'h-ref', pending_review: 'h-pend', net_economic_flow: 'h-net' },
    trend: [
      { month: '2026-01', spending: money('50.00'), fees: money('0.50'), income: money('0'), complete: true },
      { month: '2026-02', spending: money('27.50'), fees: money('0'), income: money('0'), complete: true },
    ],
    category_changes: {
      increases: [],
      decreases: [{ category: 'Food & Dining / Groceries', current: money('27.50'), previous: money('50.00'), delta: money('-22.50'), pct: '-45.0', new_in_period: false, handle: 'h-cat' }],
      restated_using_current_categories: true,
    },
    merchant_ranking: [{ merchant: 'ALPHA', value: money('20.00'), count: 1, handle: 'h-alpha' }],
    contributors_handle: 'h-all',
    ...over,
  }
}

const contributors = {
  generation: 'sha256:abc',
  scope: { lens: 'recorded_flow', currency: 'SGD', start: '2026-02-01', end: '2026-02-28', buckets: ['spending'], sort: 'amount', category: null, merchant: null },
  components: [
    { component_id: 'c1', origin_component_id: 'oc1', entry_anchor: 'a1', role: 'amount', bucket: 'spending', value: '-20.00', currency: 'SGD', category: 'Food & Dining / Groceries', merchant: 'ALPHA', merchant_key: null, account: 'Revolut', date: '2026-02-05', precision: 'DAY', source_location: 'Lines 1', nets: true },
  ],
  next_cursor: null,
  total: 1,
}

const apiMock = vi.fn(async (path: string) => {
  if (path.startsWith('analysis?')) return analysis()
  if (path.startsWith('contributors/')) return contributors
  throw new Error(`unmocked ${path}`)
})
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string) => apiMock(path) }
})

async function mount() {
  const { AnalysisView } = await import('./index')
  return render(
    <AnalysisView
      generation="sha256:abc"
      start="2026-02-01"
      end="2026-02-28"
      currency="SGD"
      accounts={['Revolut']}
      categories={['Food & Dining / Groceries']}
      onOpenTransactions={() => {}}
    />,
  )
}

afterEach(() => { cleanup(); apiMock.mockClear() })

describe('AnalysisView', () => {
  it('shows the spending comparison with delta and percentage', async () => {
    await mount()
    expect((await screen.findAllByText('SGD 27.50')).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/45\.0%/).length).toBeGreaterThan(0)
    expect(screen.getByText(/was SGD 50\.00/)).toBeInTheDocument()
  })

  it('drills a category change into its exact contributors', async () => {
    await mount()
    const row = await screen.findByText('Groceries', { selector: '.change-name' })
    fireEvent.click(row)
    await waitFor(() => expect(screen.getByText('EXACT CONTRIBUTORS')).toBeInTheDocument())
    expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('contributors/h-cat'))
    expect(screen.getByText('ALPHA', { selector: '.transactions-table td' })).toBeInTheDocument()
  })

  it('refetches when the comparison filter changes', async () => {
    await mount()
    await screen.findAllByText('SGD 27.50')
    fireEvent.change(screen.getByLabelText('Comparison period'), { target: { value: 'none' } })
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('comparison=none')),
    )
  })
})
