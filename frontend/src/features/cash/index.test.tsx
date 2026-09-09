import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const loadCashPosition = vi.fn()
const recordBalanceObservation = vi.fn()
const retireBalanceObservation = vi.fn()
vi.mock('./api', () => ({ loadCashPosition: () => loadCashPosition(), recordBalanceObservation: (...a: unknown[]) => recordBalanceObservation(...a), retireBalanceObservation: (...a: unknown[]) => retireBalanceObservation(...a) }))
vi.mock('@/lib/api', () => ({ money: (v: string, c = 'SGD') => `${c} ${v}`, ApiError: class ApiError extends Error {} }))

const { CashView } = await import('./index')

function pocket(over: Record<string, unknown> = {}) {
  return {
    account_id: 'acc-1', institution: 'Revolut', display_name: 'Revolut Current', source_type: 'REVOLUT_CSV',
    currency_pocket: 'SGD', liquid: true, excluded_reason: null, basis: 'statement-running-balance',
    balance: '360.00', as_of: '2026-03-05', date_precision: 'DAY', stale: false, staleness_days: 7,
    unavailable_reason: null, statement_running_balance: null, manual_observation: null,
    derived_balance: '360.00', derived_as_of: '2026-03-05', derived_basis: 'actual-movement-bridge', coverage_status: 'supported',
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

function position(over: Record<string, unknown> = {}) {
  return {
    generated_for: '2026-05-01', stale_after_days: 14, supported_cutoff: '2026-03-05',
    current_position_supported: false,
    current_position_reasons: ['complete actual movements from the supported cutoff through today are unavailable'],
    included_pockets: [pocket()], pockets: [pocket()],
    combined_liquid_position: { SGD: '360.00' }, liquid_total: { SGD: '360.00' },
    liquid_total_withheld: {},
    stale: false, missing_intervals: [],
    cash_bridge: { pockets: [bridge()], combined: { by_currency: { SGD: { opening: '900.00', external_inflows: '0', external_outflows: '540.00', closing: '360.00', reconciled: true, owned_transfer_pairs_cancelled: 0 } }, owned_transfer_pairs_cancelled: 0, policy: 'p' } },
    ...over,
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('CashView', () => {
  it('labels a historical common cutoff and withholds the available-today figure', async () => {
    loadCashPosition.mockResolvedValue(position())
    render(<CashView generation="gen-1" />)

    expect(await screen.findByText('Not observed as of today')).toBeInTheDocument()
    expect(screen.getByText('Observed through 2026-03-05')).toBeInTheDocument()
    expect(screen.getByText(/complete actual movements from the supported cutoff through today are unavailable/)).toBeInTheDocument()
    // No "available today" figure, reserve verdict or spending allowance without today coverage.
    expect(screen.queryByText(/available today/i)).toBeNull()
    expect(screen.getByText(/not reported as today’s cash/)).toBeInTheDocument()
  })

  it('shows a withheld currency total with its reason and never a zero', async () => {
    loadCashPosition.mockResolvedValue(position({
      supported_cutoff: null,
      combined_liquid_position: {}, liquid_total: {},
      liquid_total_withheld: { SGD: 'withheld: no common supported cutoff' },
      current_position_reasons: ['included liquid pockets do not share a supported balance cutoff'],
    }))
    render(<CashView generation="gen-1" />)

    expect(await screen.findByText('withheld: no common supported cutoff', { exact: false })).toBeInTheDocument()
    expect(screen.getByText(/1 currency withheld/)).toBeInTheDocument()
    expect(screen.queryByText('SGD 0')).toBeNull()
    expect(screen.queryByText('SGD 0.00')).toBeNull()
  })

  it('records a manual balance observation and refreshes the position', async () => {
    loadCashPosition.mockResolvedValue(position())
    recordBalanceObservation.mockResolvedValue({ status: 'recorded' })
    const onSaved = vi.fn()
    render(<CashView generation="gen-1" onSaved={onSaved} />)

    fireEvent.click(await screen.findByRole('button', { name: /Record manual balance/ }))
    expect(screen.getByText(/A dated fact asserted directly/)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Balance'), { target: { value: '0.00' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'USD sub-pocket is unused' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save observation' }))

    await waitFor(() => expect(recordBalanceObservation).toHaveBeenCalledWith('gen-1', expect.objectContaining({
      account_id: 'acc-1', currency_pocket: 'SGD', balance: '0.00', reason: 'USD sub-pocket is unused',
    })))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))
    expect(loadCashPosition).toHaveBeenCalledTimes(2)
  })

  it('offers to retire an existing manual observation', async () => {
    loadCashPosition.mockResolvedValue(position({
      pockets: [pocket({ manual_observation: { balance: '0.00', as_of: '2026-03-01', basis: 'manual-observation', observation: 'obs-1' } })],
    }))
    retireBalanceObservation.mockResolvedValue({ status: 'recorded' })
    render(<CashView generation="gen-1" />)

    fireEvent.click(await screen.findByRole('button', { name: /Update/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Retire this one' }))
    fireEvent.change(screen.getByLabelText('Reason to retire'), { target: { value: 'entered by mistake' } })
    fireEvent.click(screen.getByRole('button', { name: 'Retire observation' }))

    await waitFor(() => expect(retireBalanceObservation).toHaveBeenCalledWith('gen-1', 'obs-1', 'entered by mistake'))
  })
})
