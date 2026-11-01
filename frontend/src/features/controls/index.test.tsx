import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const load = vi.fn()
const run = vi.fn()
vi.mock('./api', () => ({
  loadSpendingControls: (...args: unknown[]) => load(...args),
  runSpendingWhatIf: (...args: unknown[]) => run(...args),
}))
vi.mock('@/lib/api', () => ({
  money: (value: string | null | undefined, currency = 'SGD') => value == null ? 'Unavailable' : `${value} ${currency}`,
  shortCategory: (value: string) => value.split(' / ').at(-1) || value,
}))

const { ControlsView } = await import('./index')

function page(scenario: unknown = null) {
  return {
    available: true,
    month: '2026-02', currency: 'SGD',
    horizon: { supported_cash_cutoff: '2026-01-31', end: '2026-07-31' },
    pace: { coverage_through_observed_date: true, budgets: [{
      plan: 'budget-1', name: 'Dining', category_key: 'Food & Dining / Dining Out',
      limit: '280.00', actual: '140.00', remaining: null, status: 'observed_so_far',
      calendar_elapsed_percent: '50.0', budget_consumed_percent: '50.0',
      coverage: { supported: true, badge: 'Covered through observed date' },
      pace_projected_spend: '280.00', pace_estimated_headroom: '0.00',
    }] },
    commitments: {
      groups: {
        mandatory_lifestyle: { amount: '600.00', cash_effect: 'external_cash_outflow', items: [] },
        debts_and_cards: { amount: '100.00', cash_effect: 'external_cash_outflow', items: [] },
        important_commitments: { amount: '200.00', cash_effect: 'external_cash_outflow', items: [] },
        planned_allocations: { amount: '50.00', cash_effect: 'internal_earmark', items: [] },
      },
      external_cash_outflows: '900.00', internal_earmarks: '50.00', distinct_source_ids: 4, disjoint: true,
    },
    free_cash_after_commitments: { amount: '50.00', formula: 'total cash − commitments', definitive_allowance: false },
    support_gates: {},
    capacity: { available: false, status: 'unresolved', reasons: ['one obligation has no date'], minimum_conservative_unallocated_reserve_adjusted_balance: '25.00', additional_spending_capacity: null, funding_need: null, weekly: null },
    first_breach_drivers: null,
    what_if_options: {
      categories: [{ category_id: 'role:SGD:Food & Dining / Dining Out', category_key: 'Food & Dining / Dining Out', tier: 4, source_backed_monthly: '140.00', maximum_suggested_monthly_reduction: '140.00' }],
      events: [],
    },
    scenario,
    unresolved: ['one obligation has no date'],
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('ControlsView', () => {
  it('submits a source-backed Tier 3/4 cut and labels the comparison hypothetical', async () => {
    load.mockResolvedValue(page())
    run.mockResolvedValue(page({
      hypothetical: true, confirmed: false,
      statement: 'Proposed only. No cut, date change or income has happened or been recorded.',
      comparison: {
        base: { before_gap: '50.00', after_gap: '30.00', cash_improvement: '20.00', unchanged_case_preserved: true },
        conservative: { before_gap: '80.00', after_gap: '60.00', cash_improvement: '20.00', unchanged_case_preserved: true },
      },
      unresolved: ['income timing remains unresolved'],
    }))
    render(<ControlsView initialMonth="2026-02" currency="SGD" />)

    expect(await screen.findByText('Spending versus this month’s calendar')).toBeInTheDocument()
    expect(screen.getByText(/Strict remaining: unavailable/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Source-backed category'), { target: { value: 'role:SGD:Food & Dining / Dining Out' } })
    fireEvent.change(screen.getByLabelText(/Proposed monthly reduction/), { target: { value: '20.00' } })
    fireEvent.click(screen.getByRole('button', { name: 'Compare before and after' }))

    await waitFor(() => expect(run).toHaveBeenCalledWith(expect.objectContaining({
      category_cuts: [{ category_id: 'role:SGD:Food & Dining / Dining Out', monthly_reduction: '20.00' }],
    })))
    expect(await screen.findByText('Hypothetical result only')).toBeInTheDocument()
    expect(screen.getByText(/No cut, date change or income has happened/)).toBeInTheDocument()
    expect(screen.getByText('income timing remains unresolved')).toBeInTheDocument()
  })
})
