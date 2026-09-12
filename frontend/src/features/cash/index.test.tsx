import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const loadReportedCash = vi.fn()
const loadReportingPolicy = vi.fn()
const recordBalanceObservation = vi.fn()
const retireBalanceObservation = vi.fn()
vi.mock('./api', () => ({
  recordBalanceObservation: (...a: unknown[]) => recordBalanceObservation(...a),
  retireBalanceObservation: (...a: unknown[]) => retireBalanceObservation(...a),
}))
vi.mock('./reportedApi', () => ({
  emptyCashFilters: { pocketCurrency: '' },
  cashScopeKey: (o: unknown) => JSON.stringify(o),
  loadReportedCash: (...a: unknown[]) => loadReportedCash(...a),
  loadReportingPolicy: (...a: unknown[]) => loadReportingPolicy(...a),
}))
vi.mock('@/lib/api', async () => {
  class ApiError extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status } }
  return { money: (v: string, c = 'SGD') => `${c} ${v}`, ApiError }
})

const { CashView } = await import('./index')

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

function pocket(over: Record<string, unknown> = {}) {
  return {
    account_id: 'acc-1', institution: 'Revolut', display_name: 'Revolut Current', source_type: 'REVOLUT_CSV',
    currency_pocket: 'SGD', liquid: true, excluded_reason: null, basis: 'statement-running-balance',
    balance: '360.00', as_of: '2026-03-05', date_precision: 'DAY', stale: false, staleness_days: 7,
    unavailable_reason: null, statement_running_balance: null, manual_observation: null,
    derived_balance: '360.00', derived_as_of: '2026-03-05', derived_basis: 'actual-movement-bridge', coverage_status: 'supported',
    reported_balance: valuedMoney(), reported_derived_balance: valuedMoney(),
    ...over,
  }
}

function bridge(over: Record<string, unknown> = {}) {
  return {
    account_id: 'acc-1', currency_pocket: 'SGD', opening: '900.00', opening_as_of: '2025-12-31',
    external_inflows: '0', external_outflows: '540.00', owned_transfer_inflows: '0', owned_transfer_outflows: '0',
    closing: '360.00', closing_as_of: '2026-03-05', calculated_closing: '360.00', reconciled: true,
    movement_count: 3, paired_transfer_link_count: 0, unsupported_reason: null,
    ...over,
  }
}

function page(over: Record<string, unknown> = {}) {
  return {
    generation: 'gen-1', reporting_currency: 'SGD', policy_version: 'v1', as_of: '2026-05-01', pocket_currency: null,
    generated_for: '2026-05-01', stale_after_days: 14, supported_cutoff: '2026-03-05',
    current_position_supported: false,
    current_position_reasons: ['complete actual movements from the supported cutoff through today are unavailable'],
    combined_liquid_position: { SGD: '360.00' }, liquid_total: { SGD: '360.00' },
    liquid_total_withheld: {},
    combined: {
      amount: { total: { value: '360.00', currency: 'SGD' }, status: 'available', partial_total: null, included: [], excluded: [], excluded_entry_count: 0, unavailable_reasons: [], excluded_detail: [], rate_path: [] },
      balance_as_of: '2026-03-05', valuation_as_of: '2026-03-05',
    },
    pockets: [pocket()],
    stale: false, missing_intervals: [],
    cash_bridge: { pockets: [bridge()], combined_by_currency: { SGD: { opening: '900.00', external_inflows: '0', external_outflows: '540.00', closing: '360.00', reconciled: true, owned_transfer_pairs_cancelled: 0 } }, owned_transfer_pairs_cancelled: 0, policy: 'p' },
    ...over,
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('CashView', () => {
  it('labels a historical common cutoff and withholds the available-today figure', async () => {
    loadReportedCash.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    render(<CashView generation="gen-1" />)

    expect(await screen.findByText('Not observed as of today')).toBeInTheDocument()
    expect(screen.getByText('Observed through 2026-03-05')).toBeInTheDocument()
    expect(screen.getByText(/complete actual movements from the supported cutoff through today are unavailable/)).toBeInTheDocument()
    expect(screen.queryByText(/available today/i)).toBeNull()
    expect(screen.getByText(/not reported as today’s cash/)).toBeInTheDocument()
    expect(screen.getByText('Reporting in SGD')).toBeInTheDocument()
  })

  it('shows a withheld currency total with its reason and never a zero', async () => {
    loadReportedCash.mockResolvedValue(page({
      supported_cutoff: null,
      combined_liquid_position: {}, liquid_total: {},
      liquid_total_withheld: { SGD: 'withheld: no common supported cutoff' },
      current_position_reasons: ['included liquid pockets do not share a supported balance cutoff'],
    }))
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    render(<CashView generation="gen-1" />)

    expect(await screen.findByText('withheld: no common supported cutoff', { exact: false })).toBeInTheDocument()
    expect(screen.getByText(/1 currency withheld/)).toBeInTheDocument()
    expect(screen.queryByText('SGD 0')).toBeNull()
    expect(screen.queryByText('SGD 0.00')).toBeNull()
  })

  it('records a manual balance observation and refreshes the position', async () => {
    loadReportedCash.mockResolvedValue(page())
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    recordBalanceObservation.mockResolvedValue({ status: 'recorded' })
    const onSaved = vi.fn()
    render(<CashView generation="gen-1" onSaved={onSaved} />)

    fireEvent.click(await screen.findByRole('button', { name: /Record manual balance/ }))
    expect(screen.getByText(/A dated fact asserted directly/)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Balance (SGD)'), { target: { value: '0.00' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'USD sub-pocket is unused' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save observation' }))

    await waitFor(() => expect(recordBalanceObservation).toHaveBeenCalledWith('gen-1', expect.objectContaining({
      account_id: 'acc-1', currency_pocket: 'SGD', balance: '0.00', reason: 'USD sub-pocket is unused',
    })))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))
    expect(loadReportedCash).toHaveBeenCalledTimes(2)
  })

  it('offers to retire an existing manual observation', async () => {
    loadReportedCash.mockResolvedValue(page({
      pockets: [pocket({ manual_observation: { balance: '0.00', as_of: '2026-03-01', basis: 'manual-observation', observation: 'obs-1' } })],
    }))
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    retireBalanceObservation.mockResolvedValue({ status: 'recorded' })
    render(<CashView generation="gen-1" />)

    fireEvent.click(await screen.findByRole('button', { name: /Update/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Retire this one' }))
    fireEvent.change(screen.getByLabelText('Reason to retire'), { target: { value: 'entered by mistake' } })
    fireEvent.click(screen.getByRole('button', { name: 'Retire observation' }))

    await waitFor(() => expect(retireBalanceObservation).toHaveBeenCalledWith('gen-1', 'obs-1', 'entered by mistake'))
  })

  it('shows an explicit, clearable Account-currency filter that narrows pockets', async () => {
    loadReportedCash.mockResolvedValue(page({
      pockets: [pocket(), pocket({ account_id: 'acc-2', currency_pocket: 'USD', balance: '200.00' })],
    }))
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    render(<CashView generation="gen-1" />)

    await screen.findByText('Reporting in SGD')
    fireEvent.change(screen.getByLabelText('Filter by account currency'), { target: { value: 'USD' } })

    await waitFor(() => expect(loadReportedCash).toHaveBeenLastCalledWith(
      expect.objectContaining({ filters: { pocketCurrency: 'USD' } }),
    ))
  })

  it('retains the last-good response on a same-scope 409 and clears on first-load stale', async () => {
    const { ApiError } = await import('@/lib/api')
    loadReportingPolicy.mockResolvedValue({ currency: 'SGD' })
    loadReportedCash.mockRejectedValueOnce(new (ApiError as unknown as { new (s: number, m: string): Error })(409, 'stale'))
    render(<CashView generation="gen-1" />)

    expect(await screen.findByText('Refresh required.')).toBeInTheDocument()

    loadReportedCash.mockResolvedValueOnce(page())
    fireEvent.click(screen.getAllByRole('button', { name: 'Refresh' })[0])
    await screen.findByText('Not observed as of today')

    loadReportedCash.mockRejectedValueOnce(new (ApiError as unknown as { new (s: number, m: string): Error })(409, 'stale'))
    fireEvent.click(screen.getAllByRole('button', { name: 'Refresh' })[0])
    expect(await screen.findByText('Showing the last loaded values for this exact scope.')).toBeInTheDocument()
    expect(screen.getByText('Not observed as of today')).toBeInTheDocument()
  })
})
