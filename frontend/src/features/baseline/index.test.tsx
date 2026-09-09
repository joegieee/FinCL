import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const loadBaselineReadiness = vi.fn()
vi.mock('./api', () => ({ loadBaselineReadiness: () => loadBaselineReadiness() }))

const { BaselineView } = await import('./index')

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
    render(<BaselineView />)

    expect(await screen.findByText('Limited history')).toBeInTheDocument()
    expect(screen.getByText(/only 1 consecutive established month/)).toBeInTheDocument()
    expect(screen.getByText('Import missing statements')).toBeInTheDocument()
    expect(screen.getByText(/2026-02-01 → 2026-02-28/)).toBeInTheDocument()
    // The month grid renders every classified month with its state.
    expect(screen.getByTitle(/2026-02 · Missing/)).toBeInTheDocument()
    expect(screen.getAllByText('Established').length).toBeGreaterThanOrEqual(2)
  })

  it('reports a clean bill when there are no evidence tasks', async () => {
    loadBaselineReadiness.mockResolvedValue(readiness({
      tasks: [],
      baseline_window: { quality: 'normal', reason: '6 consecutive fully-established months; using the latest 6', start_month: '2026-01', end_month: '2026-06', month_count: 6, basis: 'all-established' },
    }))
    render(<BaselineView />)

    expect(await screen.findByText('Normal baseline')).toBeInTheDocument()
    expect(screen.getByText(/You can derive a baseline/)).toBeInTheDocument()
  })
})
