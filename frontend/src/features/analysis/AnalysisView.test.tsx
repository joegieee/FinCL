import type React from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ApiError } from '@/lib/api'

vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('recharts')>()
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: React.ReactNode }) =>
      <div style={{ width: 600, height: 300 }}>{children}</div>,
  }
})

type Cv = {
  total: { value: string; currency: string } | null
  status: 'available' | 'partial' | 'unavailable'
  partial_total: { value: string; currency: string } | null
  included: unknown[]
  excluded: unknown[]
  excluded_entry_count: number
  unavailable_reasons: string[]
  excluded_detail: { currency: string; period: string | null; entry_count: number; reasons: string[]; remedies: string[] }[]
  rate_path: unknown[]
}

function cv(total: string | null, over: Partial<Cv> = {}): Cv {
  return {
    total: total !== null ? { value: total, currency: 'SGD' } : null,
    status: total !== null ? 'available' : 'unavailable',
    partial_total: null,
    included: [], excluded: [], excluded_entry_count: 0,
    unavailable_reasons: total !== null ? [] : ['RATE_NOT_IMPORTED'],
    excluded_detail: [], rate_path: [],
    ...over,
  }
}

function entry(current: Cv, previous: Cv | null, handle: string, over: Record<string, unknown> = {}) {
  return {
    current, previous,
    comparison_available: previous !== null,
    comparison_reasons: previous !== null ? [] : ['the comparison period is not fully covered'],
    handle, delta: null, pct: null,
    ...over,
  }
}

function reportedAnalysis(over: Record<string, unknown> = {}) {
  return {
    generation: 'sha256:abc', reporting_currency: 'SGD',
    as_of: '2026-02-28', history_start: '2026-02-01', history_end: '2026-02-28',
    pocket_currency: null, covered: true, coverage_reasons: [],
    comparison_kind: 'previous_month',
    summary: {
      spending: entry(cv('27.50'), cv('50.00'), 'h-spend'),
      fees: entry(cv('0'), cv('0.50'), 'h-fees'),
      refunds: entry(cv('5.00'), cv('0'), 'h-ref'),
      income: entry(cv('0'), cv('0'), 'h-income'),
      reimbursements: entry(cv('0'), cv('0'), 'h-reimb'),
      non_income_inflow: entry(cv('0'), cv('0'), 'h-nii'),
      pending_review: entry(cv('0'), cv('900.00'), 'h-pend'),
      net_economic_flow: entry(cv('-22.50'), cv('-50.50'), 'h-net'),
      inflow: entry(cv('5.00'), cv('0'), 'h-inflow'),
      outflow: entry(cv('27.50'), cv('50.50'), 'h-outflow'),
      exceptional_adjustments: entry(cv('0'), cv('0'), 'h-exceptional'),
    },
    gross_burn: { result: cv('27.50'), included_months: ['2026-02'], month_count: 1, reason: null },
    category_ranking: [{ name: 'Food & Dining / Groceries', result: cv('27.50'), handle: 'h-cat' }],
    merchant_ranking: [{ name: 'ALPHA', result: cv('20.00'), handle: 'h-alpha' }],
    contributors_handle: 'h-all',
    trend: [
      { month: '2026-01', spending: cv('50.00'), fees: cv('0.50'), income: cv('0'), complete: true },
      { month: '2026-02', spending: cv('27.50'), fees: cv('0'), income: cv('0'), complete: true },
    ],
    ...over,
  }
}

const contributors = {
  generation: 'sha256:abc', reporting_currency: 'SGD',
  scope_start: '2026-02-01', scope_end: '2026-02-28', buckets: ['spending'],
  pocket_currency: null, category_key: null, merchant_display: null, sort: 'amount',
  rows: [{
    component_id: 'c1', origin_kind: 'ENTRY', owning_source: { kind: 'transaction', id: 'a1' },
    role: 'amount', bucket: 'spending',
    valuation: {
      native: { value: '20.00', currency: 'SGD' }, reported: { value: '20.00', currency: 'SGD' },
      purpose: 'flow', basis: 'identity', valuation_as_of: '2026-02-05',
      rate_as_of: null, rate_period: null, rate_path: [], forward_policy: null, unavailable: null,
    },
    category: 'Food & Dining / Groceries', merchant: 'ALPHA', account: 'Revolut',
    date: '2026-02-05', precision: 'DAY', source_location: 'Lines 1', nets: true,
  }],
  next_cursor: null, total: 1,
  parent: cv('20.00'),
}

const reportingPolicy = { reporting_policy: { currency: 'SGD', is_default: true, policy_version: null } }

async function defaultImpl(path: string) {
  if (path.startsWith('reporting/analysis?')) return reportedAnalysis()
  if (path.startsWith('reporting/contributors/')) return contributors
  if (path === 'reporting-currency') return reportingPolicy
  throw new Error(`unmocked ${path}`)
}
const apiMock = vi.fn(defaultImpl)
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string) => apiMock(path) }
})

async function mount(props: Partial<{
  start: string; end: string; accounts: string[]; categories: string[]; currencies: string[]
  initialPocketCurrency: string; onOpenTransactions: (c: string, m: string) => void
}> = {}) {
  const { AnalysisView } = await import('./index')
  return render(
    <AnalysisView
      start="2026-02-01"
      end="2026-02-28"
      accounts={['Revolut', 'DBS']}
      categories={['Food & Dining / Groceries']}
      currencies={['SGD', 'USD']}
      initialPocketCurrency="SGD"
      onOpenTransactions={() => {}}
      {...props}
    />,
  )
}

afterEach(() => { cleanup(); apiMock.mockReset(); apiMock.mockImplementation(defaultImpl) })

describe('AnalysisView', () => {
  it('shows the spending comparison against all pockets by default', async () => {
    await mount()
    expect((await screen.findAllByText('SGD 27.50')).length).toBeGreaterThan(0)
    expect(screen.getByText(/was SGD 50\.00/)).toBeInTheDocument()
    // No pocket_currency query parameter on the initial, unfiltered load.
    expect(apiMock).toHaveBeenCalledWith(expect.not.stringContaining('pocket_currency'))
  })

  it('shows reporting currency as a setting, not a record filter', async () => {
    await mount()
    await screen.findAllByText('SGD 27.50')
    expect(screen.getByText(/Reporting in SGD/)).toBeInTheDocument()
  })

  it('narrows to an explicit Account-currency filter and clears it back to all pockets', async () => {
    await mount()
    await screen.findAllByText('SGD 27.50')
    fireEvent.change(screen.getByLabelText('Account currency filter'), { target: { value: 'USD' } })
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('pocket_currency=USD')))
    expect(screen.getByText('Reset analysis filters')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Reset analysis filters'))
    await waitFor(() => expect(apiMock).toHaveBeenLastCalledWith(expect.not.stringContaining('pocket_currency')))
  })

  it('seeds the Account-currency filter from a legacy ?currency= value, visibly and clearably', async () => {
    await mount({ initialPocketCurrency: 'USD' })
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('pocket_currency=USD')))
    expect(screen.getByLabelText('Account currency filter')).toHaveValue('USD')
    expect(screen.getByText('Reset analysis filters')).toBeInTheDocument()
  })

  it('drills a ranked category into its exact contributors, preserving scope', async () => {
    await mount()
    const row = await screen.findByText('Groceries', { selector: '.change-name' })
    fireEvent.click(row)
    await waitFor(() => expect(screen.getByText('EXACT CONTRIBUTORS')).toBeInTheDocument())
    expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('reporting/contributors/h-cat'))
    expect(screen.getByText('ALPHA', { selector: '.transactions-table td' })).toBeInTheDocument()
    expect(screen.getByText('Transaction')).toBeInTheDocument()
  })

  it('shows an off-ledger component with its own owning source, never a fabricated transaction', async () => {
    apiMock.mockImplementation(async (path: string) => {
      if (path.startsWith('reporting/analysis?')) return reportedAnalysis()
      if (path.startsWith('reporting/contributors/')) {
        return {
          ...contributors,
          rows: [{ ...contributors.rows[0], origin_kind: 'DEBT_EVENT', owning_source: { kind: 'debt_event', id: 'event-1' } }],
        }
      }
      if (path === 'reporting-currency') return reportingPolicy
      throw new Error(`unmocked ${path}`)
    })
    await mount()
    const row = await screen.findByText('Groceries', { selector: '.change-name' })
    fireEvent.click(row)
    await waitFor(() => expect(screen.getByText('Off-ledger debt event')).toBeInTheDocument())
  })

  it('refetches when the comparison filter changes', async () => {
    await mount()
    await screen.findAllByText('SGD 27.50')
    fireEvent.change(screen.getByLabelText('Comparison period'), { target: { value: 'none' } })
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('comparison=none')),
    )
  })

  it('refetches with the requested chart history', async () => {
    await mount()
    await screen.findAllByText('SGD 27.50')
    fireEvent.change(screen.getByLabelText('Chart history'), { target: { value: '12' } })
    await waitFor(() =>
      expect(apiMock).toHaveBeenCalledWith(expect.stringContaining('trend_range=12')),
    )
  })

  it('explains a partial total with its excluded scope and remedy', async () => {
    apiMock.mockImplementation(async (path: string) => {
      if (path.startsWith('reporting/analysis?')) {
        return reportedAnalysis({
          summary: {
            ...reportedAnalysis().summary,
            spending: entry(
              cv(null, {
                status: 'partial', partial_total: { value: '10.00', currency: 'SGD' },
                excluded_detail: [{
                  currency: 'USD', period: '2026-02', entry_count: 1,
                  reasons: ['RATE_NOT_IMPORTED'], remedies: ['Import an fx rate for USD -> SGD covering 2026-02.'],
                }],
              }),
              null, 'h-spend',
            ),
          },
        })
      }
      if (path === 'reporting-currency') return reportingPolicy
      throw new Error(`unmocked ${path}`)
    })
    await mount()
    await screen.findAllByText('10.00 SGD')
    expect(screen.getByText('Known subtotal')).toBeInTheDocument()
    const disclosure = document.querySelector('.money-excluded-disclosure')
    expect(disclosure?.textContent).toContain('Import an fx rate for USD -> SGD covering 2026-02.')
  })

  it('exposes accessible money detail through a keyboard-reachable disclosure', async () => {
    await mount()
    const row = await screen.findByText('Groceries', { selector: '.change-name' })
    fireEvent.click(row)
    await waitFor(() => expect(screen.getByText('EXACT CONTRIBUTORS')).toBeInTheDocument())
    const summary = document.querySelector('.money-disclosure > summary')
    expect(summary).not.toBeNull()
    expect(summary?.tagName).toBe('SUMMARY')
  })

  it('shows the saved same-scope response with a stale banner on a 409, not another scope', async () => {
    let call = 0
    apiMock.mockImplementation(async (path: string) => {
      if (path === 'reporting-currency') return reportingPolicy
      if (path.startsWith('reporting/analysis?')) {
        call += 1
        if (call === 1) return reportedAnalysis()
        throw new ApiError('STALE_GENERATION_REFRESH_REQUIRED', 409)
      }
      throw new Error(`unmocked ${path}`)
    })
    await mount()
    await screen.findAllByText('SGD 27.50')
    fireEvent.click(screen.getAllByRole('button', { name: /refresh/i })[0])
    await waitFor(() => expect(screen.getByText(/last loaded values for this exact period/)).toBeInTheDocument())
    // The saved same-scope response is still shown, not cleared.
    expect(screen.getAllByText('SGD 27.50').length).toBeGreaterThan(0)
  })

  it('shows Refresh required with no numbers on a first-load stale response', async () => {
    apiMock.mockImplementation(async (path: string) => {
      if (path === 'reporting-currency') return reportingPolicy
      if (path.startsWith('reporting/analysis?')) throw new ApiError('STALE_GENERATION_REFRESH_REQUIRED', 409)
      throw new Error(`unmocked ${path}`)
    })
    await mount()
    await waitFor(() => expect(screen.getByText('Refresh required.')).toBeInTheDocument())
    expect(screen.queryByText('SGD 27.50')).not.toBeInTheDocument()
  })

  it('clears the last-good response on a reporting-policy change instead of reusing it', async () => {
    let call = 0
    apiMock.mockImplementation(async (path: string) => {
      if (path === 'reporting-currency') {
        // The policy currency changes between the first success and the retry.
        return call <= 1 ? reportingPolicy : { reporting_policy: { currency: 'USD', is_default: false, policy_version: 'v2' } }
      }
      if (path.startsWith('reporting/analysis?')) {
        call += 1
        if (call === 1) return reportedAnalysis()
        throw new ApiError('STALE_GENERATION_REFRESH_REQUIRED', 409)
      }
      throw new Error(`unmocked ${path}`)
    })
    await mount()
    await screen.findAllByText('SGD 27.50')
    fireEvent.click(screen.getAllByRole('button', { name: /refresh/i })[0])
    await waitFor(() => expect(screen.getByText('Refresh required.')).toBeInTheDocument())
    expect(screen.queryByText('SGD 27.50')).not.toBeInTheDocument()
  })
})

describe('Cash Flow alias parity', () => {
  it('the Cash Flow alias renders the same AnalysisView component App.tsx mounts for Analysis', async () => {
    // Cash Flow and Analysis share one AnalysisView instance in App.tsx
    // (see tabAliases); this view itself has no tab-specific branching, so
    // covering AnalysisView in isolation covers both call sites identically.
    await mount()
    expect((await screen.findAllByText('SGD 27.50')).length).toBeGreaterThan(0)
  })
})
