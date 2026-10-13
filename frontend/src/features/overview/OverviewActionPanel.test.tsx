import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { Dashboard } from '@/lib/api'

// Release C: the position glance must not narrow to the reporting currency's
// own pocket. Every pocket appears, each listed separately, never summed.
const reserve = (amount: string) => ({ available: true, months: '3', essential_monthly: '1.00', amount, basis: 'three months of essential spend' })
vi.mock('@/features/baseline/api', () => ({
  loadBaselineEstimate: async () => ({ available: true, by_currency: {
    SGD: { historical_surplus_monthly: '100.00', reserve: reserve('300.00') },
    USD: { historical_surplus_monthly: '40.00', reserve: reserve('90.00') },
  } }),
  loadBaselineReadiness: async () => ({ pockets: [], tasks: [] }),
}))
vi.mock('@/features/obligations/api', () => ({ loadObligations: async () => ({ obligations: [], totals_by_currency: {
  USD: { bank_withdrawal: '25.00', via_card_statement: '0.00' },
} }) }))
vi.mock('@/features/networth/reportedApi', () => ({ loadReportedNetWorth: async () => ({ totals: { net_worth: { total: null, partial_total: { value: '500.00', currency: 'SGD' } } } }) }))
vi.mock('@/features/cash/api', () => ({ loadCashPosition: async () => ({ current_position_supported: false, current_position_reasons: [], liquid_total: {}, generated_for: '2026-01-31' }) }))
vi.mock('@/features/forecast/api', () => ({ loadForecast: async () => ({ available: false, reason: 'x', by_currency: {} }) }))
vi.mock('@/features/planning/api', () => ({ loadRecurring: async () => ({ candidates: [] }) }))
vi.mock('@/features/setup/api', () => ({ loadImports: async () => ({ imports: [] }) }))

afterEach(cleanup)

describe('OverviewActionPanel', () => {
  it('shows every currency pocket rather than only the reporting currency’s own', async () => {
    const { OverviewActionPanel } = await import('./index')
    render(<OverviewActionPanel data={{ generation: 'g', review_count: 0 } as Dashboard} currency="SGD" onOpen={() => {}} />)
    expect((await screen.findAllByText(/USD 40\.00 \(USD pocket\)/)).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/SGD 100\.00 \(SGD pocket\)/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/USD 90\.00 \(USD pocket\)/).length).toBeGreaterThan(0)
    // A pocket with obligations is shown even though it is not the reporting currency.
    expect(screen.getByText(/USD 25\.00 bank/)).toBeInTheDocument()
    expect(screen.getByText(/500\.00 known so far/)).toBeInTheDocument()
  })
})
