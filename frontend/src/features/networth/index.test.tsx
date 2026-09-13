import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const loadReportedNetWorth = vi.fn()
const loadReportingPolicy = vi.fn()
vi.mock('./reportedApi', () => ({
  netWorthScopeKey: (o: unknown) => JSON.stringify(o),
  loadReportedNetWorth: (...a: unknown[]) => loadReportedNetWorth(...a),
  loadReportingPolicy: (...a: unknown[]) => loadReportingPolicy(...a),
}))
vi.mock('@/lib/api', async () => {
  class ApiError extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status } }
  return { money: (v: string, c = 'SGD') => `${c} ${v}`, ApiError }
})

const { NetWorthView } = await import('./index')

function cv(over: Record<string, unknown> = {}) {
  return {
    total: { value: '360.00', currency: 'SGD' }, status: 'available', partial_total: null,
    included: [{ currency: 'SGD', period: '2026-03-05' }], excluded: [], excluded_entry_count: 0,
    unavailable_reasons: [], excluded_detail: [], rate_path: [],
    ...over,
  }
}

function valuedMoney(over: Record<string, unknown> = {}) {
  return {
    native: { value: '360.00', currency: 'SGD' },
    reported: { value: '360.00', currency: 'SGD' },
    purpose: 'position', basis: 'identity', valuation_as_of: '2026-03-05',
    rate_as_of: null, rate_period: null, rate_path: [], forward_policy: null,
    unavailable: null,
    ...over,
  }
}

function component(over: Record<string, unknown> = {}) {
  return {
    component_id: 'cash:acc-1:SGD', kind: 'cash', side: 'asset', currency: 'SGD',
    amount: '360.00', as_of: '2026-03-05', basis: 'evidence', origin: 'position · statement-running-balance',
    label: 'Revolut Current', included: true, confidence: null, note: null,
    reported: valuedMoney(),
    ...over,
  }
}

function currencySummary(over: Record<string, unknown> = {}) {
  return {
    tracked_assets: '360.00', tracked_liabilities: '0.00', tracked_net_worth: '360.00',
    manual_assets: '0.00', manual_liabilities: '0.00', net_worth_including_manual: '360.00',
    manual_basis_note: 'manual note', subtotal_note: 'tracked subtotal only',
    missing_components: [], excluded_components: [], complete: true,
    valuation_dates: ['2026-03-05'], as_of: '2026-03-05',
    ...over,
  }
}

function page(over: Record<string, unknown> = {}) {
  return {
    generation: 'gen-1', reporting_currency: 'SGD', policy_version: 'v1', as_of: '2026-03-06',
    prior_as_of: null, label: 'tracked net worth', is_total_personal_wealth: false,
    disclaimer: 'A per-currency subtotal ... not proof of total personal wealth and is not labelled as such.',
    notes: ['each figure from exactly one upstream read'],
    components: [component()],
    totals: {
      assets: cv(), liabilities: cv({ total: { value: '0.00', currency: 'SGD' }, included: [] }),
      net_worth: cv(),
      manual_assets: cv({ total: { value: '0.00', currency: 'SGD' }, included: [] }),
      manual_liabilities: cv({ total: { value: '0.00', currency: 'SGD' }, included: [] }),
      net_worth_including_manual: cv(),
    },
    trend: null,
    per_currency: { SGD: currencySummary() },
    ...over,
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('NetWorthView', () => {
  it('shows the reported tracked subtotal beside the native per-currency card', async () => {
    loadReportedNetWorth.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    render(<NetWorthView />)

    expect(await screen.findByText('This is a tracked subtotal, not your net worth.')).toBeInTheDocument()
    expect(screen.getByText('SGD · REPORTED TRACKED SUBTOTAL')).toBeInTheDocument()
    expect(screen.getByText('SGD · NATIVE EVIDENCE-ONLY SUBTOTAL')).toBeInTheDocument()
    expect(screen.getAllByText(/360\.00/).length).toBeGreaterThan(0)
  })

  it('shows a missing component as a named gap, never a zero, with no reported evidence', async () => {
    loadReportedNetWorth.mockResolvedValue(page({
      components: [component({
        component_id: 'cash:b:SGD', amount: null, as_of: null, included: false,
        note: 'no supported balance boundary', reported: null,
      })],
      per_currency: {
        SGD: currencySummary({
          complete: false,
          missing_components: [{ component_id: 'cash:b:SGD', kind: 'cash', side: 'asset', reason: 'no supported balance boundary' }],
        }),
      },
    }))
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    render(<NetWorthView />)

    expect(await screen.findByText('Missing')).toBeInTheDocument()
    expect(screen.getByText('No evidence')).toBeInTheDocument()
    expect(screen.getAllByText(/no supported balance boundary/).length).toBeGreaterThan(0)
    expect(screen.getByText('Known subtotal — components missing')).toBeInTheDocument()
  })

  it('applies a prior date and requests the reported endpoint with both dates', async () => {
    loadReportedNetWorth.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    render(<NetWorthView />)
    await screen.findByText('This is a tracked subtotal, not your net worth.')

    fireEvent.change(screen.getByLabelText('As of date'), { target: { value: '2026-03-06' } })
    fireEvent.change(screen.getByLabelText('Prior observation date'), { target: { value: '2026-02-06' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(loadReportedNetWorth).toHaveBeenLastCalledWith({ asOf: '2026-03-06', priorAsOf: '2026-02-06' }))
  })

  it('renders a reported trend delta and flags an incomplete comparison', async () => {
    loadReportedNetWorth.mockResolvedValue(page({
      prior_as_of: '2026-02-06',
      trend: {
        prior_as_of: '2026-02-06', current_as_of: '2026-03-06',
        prior_net_worth: cv({ total: { value: '860.00', currency: 'SGD' } }),
        current_net_worth: cv(),
        delta: { value: '-500.00', currency: 'SGD' },
        both_complete: true, scope_changed: false,
        scope_changes: [], comparability: 'the two observations cover the same tracked components',
      },
    }))
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    render(<NetWorthView />)

    expect(await screen.findByText(/Δ SGD -500\.00/)).toBeInTheDocument()
    expect(screen.getByText(/same tracked components/)).toBeInTheDocument()
  })

  it('retains the last-good response on a same-scope 409 and clears on first-load stale', async () => {
    const { ApiError } = await import('@/lib/api')
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadReportedNetWorth.mockRejectedValueOnce(new (ApiError as unknown as { new (s: number, m: string): Error })(409, 'stale'))
    render(<NetWorthView />)

    expect(await screen.findByText('Refresh required.')).toBeInTheDocument()

    loadReportedNetWorth.mockResolvedValueOnce(page())
    fireEvent.click(screen.getAllByRole('button', { name: 'Refresh' })[0])
    await screen.findByText('This is a tracked subtotal, not your net worth.')

    loadReportedNetWorth.mockRejectedValueOnce(new (ApiError as unknown as { new (s: number, m: string): Error })(409, 'stale'))
    fireEvent.click(screen.getAllByRole('button', { name: 'Refresh' })[0])
    expect(await screen.findByText('Showing the last loaded values for this exact scope.')).toBeInTheDocument()
    expect(screen.getByText('This is a tracked subtotal, not your net worth.')).toBeInTheDocument()
  })
})
