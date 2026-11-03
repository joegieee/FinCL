import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const month = (over: Record<string, unknown> = {}) => ({
  month: '2026-02', period_status: 'scored', scored: true, net_cash_effect: '-100.00',
  open_expected: { receipts: '0.00', payments: '0.00' },
  modelled_spending: { expected: '0.00', actual: '0.00' },
  unclassified_actual: { receipts: '0.00', payments: '0.00' },
  ...over,
})

function page(months: ReturnType<typeof month>[], events: unknown[] = []) {
  return {
    snapshot: { snapshot: 'snap-1', issued_at: '2026-01-15', digest_verified: true, case: 'base' },
    as_of: '2026-03-02', currency: 'SGD', coverage: { covered: true, status: 'COVERED', reasons: [] },
    months, events, unmatched_movements: [], biggest_misses: [], match_suggestions: [],
    likely_income_realization: { available: false, sample_count: 0, issued_occurrences: 0, occurrence_rate: null, expected_amount: null, realized_amount: null, auto_applied: false, reason: 'none yet' },
  }
}

const loadVariance = vi.fn()
const retireMatch = vi.fn()
vi.mock('./api', () => ({
  loadSnapshots: () => Promise.resolve({ snapshots: [{ snapshot: 'snap-1', issued_at: '2026-01-15', digest: 'd', journal_seq: 1 }] }),
  loadVariance: (...a: unknown[]) => loadVariance(...a),
  saveSnapshot: vi.fn(),
  recordMatch: vi.fn(),
  retireMatch: (...a: unknown[]) => retireMatch(...a),
}))

const { VarianceView } = await import('./index')

async function compare() {
  render(<VarianceView generation="g" currency="SGD" onSaved={() => {}} />)
  const button = await screen.findByRole('button', { name: 'Compare' })
  await waitFor(() => expect(button).toBeEnabled())
  fireEvent.click(button)
  await screen.findByText('2026-02')
}

describe('VarianceView months', () => {
  afterEach(() => { cleanup(); loadVariance.mockReset(); retireMatch.mockReset() })

  it('says how much of a month is still open', async () => {
    loadVariance.mockResolvedValue(page([month({ open_expected: { receipts: '3000.00', payments: '0.00' } })]))
    await compare()
    expect(screen.getByText(/Still open: 3,000.00 SGD in, 0.00 SGD out/)).toBeInTheDocument()
  })

  it('says nothing when the whole month is answered', async () => {
    loadVariance.mockResolvedValue(page([month()]))
    await compare()
    expect(screen.queryByText(/Still open/)).not.toBeInTheDocument()
  })

  it('scores the allowance on its monthly total and leaves its slices out of the event list', async () => {
    const slice = { event_id: 'residual:1', label: 'Daily slice', expected: '5.00', actual: null, status: 'modelled', status_reason: 'modelled', matches: [] }
    const rent = { event_id: 'rent', label: 'Rent', expected: '1200.00', actual: null, status: 'missing', status_reason: 'gone', matches: [] }
    loadVariance.mockResolvedValue(page([month({ modelled_spending: { expected: '300.00', actual: '280.00' } })], [slice, rent]))
    await compare()
    expect(screen.getByText('280.00 SGD of 300.00 SGD')).toBeInTheDocument()
    expect(screen.getByText('Rent')).toBeInTheDocument()
    expect(screen.queryByText('Daily slice')).not.toBeInTheDocument()
  })

  it('retires a match and reloads the comparison', async () => {
    const matched = { event_id: 'rent', label: 'Rent', expected: '1200.00', actual: '1200.00', status: 'matched', status_reason: 'done', matches: [{ match: 'm1', landed_on: '2026-02-10', amount: '1200.00' }] }
    loadVariance.mockResolvedValue(page([month()], [matched]))
    retireMatch.mockResolvedValue({ status: 'applied' })
    await compare()
    fireEvent.click(screen.getByRole('button', { name: 'Retire match of Rent on 2026-02-10' }))
    await waitFor(() => expect(retireMatch).toHaveBeenCalledWith('g', 'snap-1', 'm1', expect.any(String)))
    await waitFor(() => expect(loadVariance).toHaveBeenCalledTimes(2))
  })
})
