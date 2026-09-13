import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const loadReportedForecast = vi.fn()
const loadReportingPolicy = vi.fn()
vi.mock('./reportedApi', () => ({
  loadReportedForecast: (...a: unknown[]) => loadReportedForecast(...a),
  loadReportingPolicy: (...a: unknown[]) => loadReportingPolicy(...a),
  nativeForecast: (p: { native: unknown }) => p.native,
}))
vi.mock('@/lib/api', async () => {
  class ApiError extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status } }
  return { money: (v: string, c = 'SGD') => `${c} ${v}`, ApiError }
})

const { ForecastView } = await import('./index')

function valuedMoney(nativeValue: string, reportedValue: string | null = null, currency = 'SGD') {
  return {
    native: { value: nativeValue, currency },
    reported: reportedValue === null ? null : { value: reportedValue, currency },
    purpose: 'forecast', basis: reportedValue === null ? null : 'held_latest', valuation_as_of: '2026-03-06',
    rate_as_of: null, rate_period: null, rate_path: [], forward_policy: null,
    unavailable: reportedValue === null ? { reason: 'RATE_NOT_IMPORTED', message: 'no rate', remedy: 'import a rate' } : null,
  }
}

function nativeMonth(over: Record<string, unknown> = {}) {
  return {
    month: '2026-02', partial: false, days_covered: 28, days_in_month: 28,
    opening: '100.00', income: '200.00', expense: '150.00', closing: '150.00',
    lowest_balance: { amount: '-50.00', on: '2026-02-05', precision: 'day', window: { earliest: '2026-02-05', latest: '2026-02-05' } },
    closes_positive_over_an_intra_month_low: true,
    ...over,
  }
}

function nativeScenario(over: Record<string, unknown> = {}) {
  return {
    available: true,
    months: [nativeMonth()],
    closing_balance: '150.00',
    lowest_balance: { occurs: true, on: '2026-02-05', amount: '-50.00', precision: 'day', window: { earliest: '2026-02-05', latest: '2026-02-05' }, note: 'low' },
    first_zero: { occurs: true, on: '2026-02-05', precision: 'day', window: { earliest: '2026-02-05', latest: '2026-02-05' }, note: 'zero' },
    first_reserve_breach: { available: false, reason: 'no reserve target' },
    policy: { income: 'income', spending: 'spending', upper_spend_label: 'observed stress case' },
    events: { items: [] }, limitations: [], ...over,
  }
}

function nativePage(over: Record<string, unknown> = {}) {
  const base = nativeScenario()
  return {
    available: true,
    horizon: { supported_cash_cutoff: '2026-01-31', end: '2026-07-31', months: 6, boundary_rule: 'rule' },
    by_currency: { SGD: { available: true, starting_balance: '100.00', starting_balance_as_of: '2026-01-31', base, conservative: nativeScenario(), reserve_readiness: { available: false, reason: 'no reserve' } } },
    case_policy: { base: 'base', conservative: 'conservative', strong_income_interruption: false, strong_income_interruption_note: null },
    limitations: [], ...over,
  }
}

function reportedMonth(over: Record<string, unknown> = {}) {
  return {
    month: '2026-02',
    opening: valuedMoney('100.00', '100.00'), closing: valuedMoney('150.00', '150.00'),
    income: valuedMoney('200.00', '200.00'), expense: valuedMoney('150.00', '150.00'),
    lowest_balance: valuedMoney('-50.00', '-50.00'),
    ...over,
  }
}

function reportedScenario(over: Record<string, unknown> = {}) {
  return {
    closing_balance: valuedMoney('150.00', '150.00'), lowest_balance: valuedMoney('-50.00', '-50.00'),
    months: [reportedMonth()], events: [],
    ...over,
  }
}

function cv(over: Record<string, unknown> = {}) {
  return {
    total: { value: '150.00', currency: 'SGD' }, status: 'available', partial_total: null,
    included: [{ currency: 'SGD', period: '2026-03' }], excluded: [], excluded_entry_count: 0,
    unavailable_reasons: [], excluded_detail: [], rate_path: [],
    ...over,
  }
}

function unavailableMinimum(reason = 'NO_COMBINED_MINIMUM_EVIDENCE') {
  return {
    amount: {
      total: null, status: 'unavailable', partial_total: null, included: [], excluded: [{ currency: 'USD', period: null }],
      excluded_entry_count: 1, unavailable_reasons: [reason], excluded_detail: [{ currency: 'USD', period: null, entry_count: 1, reasons: [reason], remedies: ['import a rate'] }],
      rate_path: [],
    },
    month: null,
  }
}

function page(over: Record<string, unknown> = {}) {
  return {
    generation: 'gen-1', reporting_currency: 'SGD', policy_version: 'v1', valuation_as_of: '2026-03-06',
    opening_balance_as_of: '2026-01-31', horizon_end: '2026-07-31',
    currencies: [{ currency: 'SGD', starting_balance: valuedMoney('100.00', '100.00'), base: reportedScenario(), conservative: reportedScenario() }],
    totals: {
      base_closing: cv(), conservative_closing: cv(),
      base_combined_minimum: unavailableMinimum(), conservative_combined_minimum: unavailableMinimum(),
    },
    limitations: [],
    native: nativePage(),
    ...over,
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('ForecastView', () => {
  it('shows a breach that happens inside a month even when month-end remains positive', async () => {
    loadReportedForecast.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    render(<ForecastView />)

    expect(await screen.findByText('Cash-risk breach in the forecast')).toBeInTheDocument()
    expect(screen.getAllByText(/still closes positive after an intra-month breach/)).not.toHaveLength(0)
    expect(screen.getByText('Reporting in SGD', { exact: false })).toBeInTheDocument()
  })

  it('renders the combined totals card and an unavailable combined minimum with its reason', async () => {
    loadReportedForecast.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    render(<ForecastView />)

    expect(await screen.findByText('Six-month closing and worst point')).toBeInTheDocument()
    expect(screen.getAllByText(/NO_COMBINED_MINIMUM_EVIDENCE|Not available/).length).toBeGreaterThan(0)
  })

  it('renders an unavailable native currency as a named gap with its reason', async () => {
    loadReportedForecast.mockResolvedValue(page({
      currencies: [],
      native: nativePage({ available: false, reason: 'no supported opening cash', by_currency: { SGD: { available: false, reason: 'missing reconciled liquid balance' } } }),
    }))
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    render(<ForecastView />)

    expect(await screen.findAllByText('Forecast gap')).not.toHaveLength(0)
    expect(screen.getByText(/missing reconciled liquid balance/)).toBeInTheDocument()
  })

  it('shows a missing period, not NaN, when the conservative case is shorter than the base case', async () => {
    const shortConservative = reportedScenario({ months: [reportedMonth()] })
    const longBase = reportedScenario({ months: [reportedMonth(), reportedMonth({ month: '2026-03', closing: valuedMoney('90.00', '90.00') })] })
    loadReportedForecast.mockResolvedValue(page({
      currencies: [{ currency: 'SGD', starting_balance: valuedMoney('100.00', '100.00'), base: longBase, conservative: shortConservative }],
    }))
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    render(<ForecastView />)

    await screen.findByText('Balance by month')
    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument()
    expect(screen.getAllByText('Missing period').length).toBeGreaterThan(0)
  })

  it('retains the last-good response on a same-scope 409 and clears on first-load stale', async () => {
    const { ApiError } = await import('@/lib/api')
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadReportedForecast.mockRejectedValueOnce(new (ApiError as unknown as { new (s: number, m: string): Error })(409, 'stale'))
    render(<ForecastView />)

    expect(await screen.findByText('Refresh required.')).toBeInTheDocument()

    loadReportedForecast.mockResolvedValueOnce(page())
    fireEvent.click(screen.getAllByRole('button', { name: 'Refresh' })[0])
    await screen.findByText('Six-month closing and worst point')

    loadReportedForecast.mockRejectedValueOnce(new (ApiError as unknown as { new (s: number, m: string): Error })(409, 'stale'))
    fireEvent.click(screen.getAllByRole('button', { name: 'Refresh' })[0])
    expect(await screen.findByText('Showing the last loaded values for this exact scope.')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('Six-month closing and worst point')).toBeInTheDocument())
  })
})
