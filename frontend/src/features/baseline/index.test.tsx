import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const loadBaselineReadiness = vi.fn()
const loadBaselineEstimate = vi.fn()
vi.mock('./api', () => ({
  loadBaselineReadiness: () => loadBaselineReadiness(),
  loadBaselineEstimate: () => loadBaselineEstimate(),
}))
vi.mock('@/lib/api', () => ({ money: (v: string, c = 'SGD') => `${c} ${v}` }))

const { BaselineView } = await import('./index')

function estimate(over: Record<string, unknown> = {}) {
  return {
    available: true,
    generation: 'g1',
    baseline_window: { quality: 'limited-history', reason: 'x', start_month: '2026-03', end_month: '2026-03', month_count: 1, basis: 'all-established' },
    currencies: ['SGD'],
    sample: { months: ['2026-03'], month_count: 1, quality: 'limited-history', method_version: 'baseline-estimator-v1', residual_method: 'arithmetic-mean-of-monthly-routine-residual', outlier_method: 'outlier-v1' },
    by_currency: {
      SGD: {
        months: [{ month: '2026-03', owner_cost_recorded: '960.00', identified_recurring: '0.00', unresolved_irregular: '900.00', adjustments: '0.00', routine_variable_residual: '60.00', reconciles: true, income: '0' }],
        fixed_recurring_monthly: '0.00', variable_residual_monthly: '60.00', variable_residual_median: '60.00',
        variable_residual_min: '60.00', variable_residual_max: '60.00', irregular_allowance_monthly: '900.00',
        routine_lifestyle_baseline: '60.00', total_observed_monthly: '960.00', net_received_income_monthly: '0.00',
        historical_surplus_monthly: '-960.00',
        savings_rate: { available: false, reason: 'no classified income in the baseline window', numerator: '-960.00', denominator: '0.00' },
        fixed_cost_ratio: { available: true, numerator: '0.00', denominator: '60.00', value: '0.0000' },
        roles: { months: [{ month: '2026-03', essential: '0.00', discretionary: '60.00', unresolved: '900.00', total: '960.00', reconciles: true }], totals: { essential: '0.00', discretionary: '60.00', unresolved: '900.00', total: '960.00', reconciles: true } },
        role_groups: [{ category_key: 'Unclassified / Unclassified', cost_pattern: 'unresolved', essential_status: 'unresolved', tier: null, basis: 'unmapped or unclassified category', review_status: 'unresolved', amount: '900.00', source_count: 1 }],
        reserve: { available: false, reason: 'essential role coverage is incomplete' },
        irregular_risk_entries: [{ month: '2026-03', currency: 'SGD', category_key: 'Unclassified / Unclassified', amount: '900.00', threshold: '180.00', review_state: 'suggested' }],
        regime: { warning: false, reasons: [], leading_empty_months: 0, earlier_mean_owner_cost: '0.00', recent_mean_owner_cost: '960.00' },
        sensitivity: { available: false, reason: 'a single-month window has nothing to exclude' },
      },
    },
    income_streams: [],
    reconciliation: { SGD: { balances: true, owner_cost_recorded_total: '960.00', owner_cost_baseline_total: '960.00' } },
    limitations: ['automatic category defaults carry their source and review status'],
    tasks: [],
    ...over,
  }
}

function readiness(over: Record<string, unknown> = {}) {
  return {
    generation: 'g1',
    generated_for: '2026-07-15',
    scope: { accounts: ['acc-1'], currencies: ['SGD'], analysis_window: { start: '2026-01-01', end: '2026-06-30' } },
    pockets: [{
      account_id: 'acc-1', institution: 'MariBank', display_name: 'Mari Savings', source_type: 'MARI_SAVINGS',
      currency_pocket: 'SGD', expected_span: null, observed_span: { start: '2026-01-03', end: '2026-06-28' },
      span: { start: '2026-01-01', end: '2026-06-30' }, entry_count: 42, reconciliation: { passed: 12, total: 12 },
      months: [
        { month: '2026-01', completeness: 'established', basis: 'statement-period', entry_count: 7 },
        { month: '2026-02', completeness: 'missing', basis: 'no-evidence', entry_count: 0 },
        { month: '2026-03', completeness: 'established', basis: 'statement-period', entry_count: 8 },
      ],
      missing_intervals: [{ start: '2026-02-01', end: '2026-02-28' }],
      warnings: ['evidence ends 2026-06-28, before the expected 2026-06-30'],
    }],
    baseline_window: { quality: 'limited-history', reason: 'only 1 consecutive established month(s)', start_month: '2026-03', end_month: '2026-03', month_count: 1, basis: 'all-established' },
    tasks: [{ kind: 'import-missing', account_id: 'acc-1', display_name: 'Mari Savings', currency_pocket: 'SGD', interval: { start: '2026-02-01', end: '2026-02-28' }, note: 'no statement or activity for Mari Savings SGD over 2026-02-01..2026-02-28' }],
    ...over,
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('BaselineView', () => {
  it('shows the recommended window, the month grid and the missing-evidence task', async () => {
    loadBaselineReadiness.mockResolvedValue(readiness())
    loadBaselineEstimate.mockResolvedValue(estimate())
    render(<BaselineView />)

    expect(await screen.findAllByText('Limited history')).not.toHaveLength(0)
    expect(screen.getByText(/only 1 consecutive established month/)).toBeInTheDocument()
    expect(screen.getByText('Import missing statements')).toBeInTheDocument()
    expect(screen.getByText(/2026-02-01 → 2026-02-28/)).toBeInTheDocument()
    expect(screen.getByTitle(/2026-02 · Missing/)).toBeInTheDocument()
    expect(screen.getAllByText('Established').length).toBeGreaterThanOrEqual(2)
  })

  it('renders the estimate decomposition and unresolved role review', async () => {
    loadBaselineReadiness.mockResolvedValue(readiness())
    loadBaselineEstimate.mockResolvedValue(estimate())
    render(<BaselineView />)

    expect(await screen.findByText(/WHAT YOUR NORMAL LIFE COSTS · SGD/)).toBeInTheDocument()
    expect(screen.getAllByText('SGD 60.00 / month')).not.toHaveLength(0)
    expect(screen.getByText(/Unavailable — essential role coverage is incomplete/)).toBeInTheDocument()
    expect(screen.getByText('Discretionary (sample total)')).toBeInTheDocument()
    expect(screen.getByText(/Unavailable — no classified income/)).toBeInTheDocument()
    expect(screen.getByText(/1 entry held out as irregular risk/)).toBeInTheDocument()
    expect(screen.getByText(/Sensitivity \(highest month removed\)/)).toBeInTheDocument()
    expect(screen.getByText(/a single-month window has nothing to exclude/)).toBeInTheDocument()
  })

  it('shows a regime warning when the window is not one steady regime', async () => {
    loadBaselineReadiness.mockResolvedValue(readiness())
    loadBaselineEstimate.mockResolvedValue(estimate({
      by_currency: {
        SGD: {
          ...estimate().by_currency.SGD,
          regime: { warning: true, reasons: ['activity begins in 2026-03, after 2 empty months in the window'], leading_empty_months: 2, earlier_mean_owner_cost: '0.00', recent_mean_owner_cost: '960.00' },
        },
      },
    }))
    render(<BaselineView />)

    expect(await screen.findByText(/Regime: activity begins in 2026-03/)).toBeInTheDocument()
  })

  it('reports a clean bill when there are no evidence tasks', async () => {
    loadBaselineReadiness.mockResolvedValue(readiness({
      tasks: [],
      baseline_window: { quality: 'normal', reason: '6 consecutive fully-established months; using the latest 6', start_month: '2026-01', end_month: '2026-06', month_count: 6, basis: 'all-established' },
    }))
    loadBaselineEstimate.mockResolvedValue(estimate())
    render(<BaselineView />)

    expect(await screen.findByText('Normal baseline')).toBeInTheDocument()
    expect(screen.getByText(/You can derive a baseline/)).toBeInTheDocument()
  })
})
