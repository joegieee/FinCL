import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const loadForecast = vi.fn()
vi.mock('./api', () => ({ loadForecast: () => loadForecast() }))
vi.mock('@/lib/api', () => ({ money: (v: string, c = 'SGD') => `${c} ${v}` }))

const { ForecastView } = await import('./index')

function scenario(over: Record<string, unknown> = {}) {
  return {
    available: true,
    months: [{
      month: '2026-02', partial: false, days_covered: 28, days_in_month: 28,
      opening: '100.00', income: '200.00', expense: '150.00', closing: '150.00',
      lowest_balance: { amount: '-50.00', on: '2026-02-05', precision: 'day', window: { earliest: '2026-02-05', latest: '2026-02-05' } },
      closes_positive_over_an_intra_month_low: true,
    }],
    closing_balance: '150.00',
    lowest_balance: { occurs: true, on: '2026-02-05', amount: '-50.00', precision: 'day', window: { earliest: '2026-02-05', latest: '2026-02-05' }, note: 'low' },
    first_zero: { occurs: true, on: '2026-02-05', precision: 'day', window: { earliest: '2026-02-05', latest: '2026-02-05' }, note: 'zero' },
    first_reserve_breach: { available: false, reason: 'no reserve target' },
    policy: { income: 'income', spending: 'spending', upper_spend_label: 'observed stress case' },
    events: { items: [] }, limitations: [], ...over,
  }
}

function page(over: Record<string, unknown> = {}) {
  const base = scenario()
  return {
    available: true,
    horizon: { supported_cash_cutoff: '2026-01-31', end: '2026-07-31', months: 6, boundary_rule: 'rule' },
    by_currency: { SGD: { available: true, starting_balance: '100.00', starting_balance_as_of: '2026-01-31', base, conservative: scenario(), reserve_readiness: { available: false, reason: 'no reserve' } } },
    case_policy: { base: 'base', conservative: 'conservative', strong_income_interruption: false, strong_income_interruption_note: null },
    limitations: [], ...over,
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('ForecastView', () => {
  it('shows a breach that happens inside a month even when month-end remains positive', async () => {
    loadForecast.mockResolvedValue(page())
    render(<ForecastView />)

    expect(await screen.findByText('Cash-risk breach in the forecast')).toBeInTheDocument()
    expect(screen.getAllByText(/still closes positive after an intra-month breach/)).not.toHaveLength(0)
    expect(screen.getAllByText(/SGD 150.00/)).not.toHaveLength(0)
  })

  it('renders an unavailable output as a named gap with its reason', async () => {
    loadForecast.mockResolvedValue(page({ available: false, reason: 'no supported opening cash', by_currency: { SGD: { available: false, reason: 'missing reconciled liquid balance' } } }))
    render(<ForecastView />)

    expect(await screen.findAllByText('Forecast gap')).not.toHaveLength(0)
    expect(screen.getByText(/missing reconciled liquid balance/)).toBeInTheDocument()
    expect(screen.queryByText('SGD 0.00')).not.toBeInTheDocument()
  })

  it('shows a missing period, not NaN, when the conservative case is shorter than the base case', async () => {
    const shortConservative = scenario({ months: [scenario().months[0]] })
    const longBase = scenario({ months: [scenario().months[0], { ...scenario().months[0], month: '2026-03', closing: '90.00' }] })
    loadForecast.mockResolvedValue(page({ by_currency: { SGD: { available: true, starting_balance: '100.00', starting_balance_as_of: '2026-01-31', base: longBase, conservative: shortConservative, reserve_readiness: { available: false, reason: 'no reserve' } } } }))
    render(<ForecastView />)

    await screen.findByText('Balance by month')
    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument()
    expect(screen.getAllByText('Missing period').length).toBeGreaterThan(0)
  })

  it('renders a backend refusal payload with no case_policy without crashing', async () => {
    loadForecast.mockResolvedValue({
      available: false,
      reason: 'Stage 3B refused mixed-generation source adapters',
      by_currency: {},
      limitations: ['refresh the ledger and retry'],
    })
    render(<ForecastView />)

    expect(await screen.findAllByText(/Stage 3B refused mixed-generation source adapters/)).not.toHaveLength(0)
    expect(screen.queryByText('How this estimate was formed')).not.toBeInTheDocument()
  })
})
