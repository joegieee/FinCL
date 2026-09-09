import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const loadCashPosition = vi.fn()
vi.mock('./api', () => ({ loadCashPosition: () => loadCashPosition() }))
vi.mock('@/lib/api', () => ({ money: (v: string, c = 'SGD') => `${c} ${v}` }))

const { CashView } = await import('./index')

function pocket(over: Record<string, unknown> = {}) {
  return {
    account_id: 'acc-1', institution: 'Revolut', display_name: 'Revolut Current', source_type: 'REVOLUT_CSV',
    currency_pocket: 'SGD', liquid: true, excluded_reason: null, basis: 'statement-running-balance',
    balance: '360.00', as_of: '2026-03-05', date_precision: 'DAY', stale: false, staleness_days: 7,
    unavailable_reason: null, statement_running_balance: null, manual_observation: null,
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
  it('labels a modelled cutoff and withholds the available-today figure', async () => {
    loadCashPosition.mockResolvedValue(position())
    render(<CashView />)

    expect(await screen.findByText('Not observed as of today')).toBeInTheDocument()
    expect(screen.getByText('Modelled')).toBeInTheDocument()
    expect(screen.getByText(/complete actual movements from the supported cutoff through today are unavailable/)).toBeInTheDocument()
    // No "available today" figure, reserve verdict or spending allowance while modelled.
    expect(screen.queryByText(/available today/i)).toBeNull()
    // The last supported balance is carried forward only for reference.
    expect(screen.getByText(/Carried forward for reference only/)).toBeInTheDocument()
  })

  it('shows a withheld currency total with its reason and never a zero', async () => {
    loadCashPosition.mockResolvedValue(position({
      supported_cutoff: null,
      combined_liquid_position: {}, liquid_total: {},
      liquid_total_withheld: { SGD: 'withheld: no common supported cutoff' },
      current_position_reasons: ['included liquid pockets do not share a supported balance cutoff'],
    }))
    render(<CashView />)

    expect(await screen.findByText('withheld: no common supported cutoff', { exact: false })).toBeInTheDocument()
    expect(screen.getByText(/1 currency withheld/)).toBeInTheDocument()
    expect(screen.queryByText('SGD 0')).toBeNull()
    expect(screen.queryByText('SGD 0.00')).toBeNull()
  })
})
