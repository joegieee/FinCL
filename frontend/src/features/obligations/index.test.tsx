import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const loadObligations = vi.fn()
vi.mock('./api', () => ({ loadObligations: () => loadObligations() }))
vi.mock('@/lib/api', () => ({ money: (v: string, c = 'SGD') => `${v} ${c}` }))

const { ObligationsView } = await import('./index')

function obligation(over: Record<string, unknown> = {}) {
  return {
    obligation_id: 'debt:d1',
    origin: 'personal_debt',
    source: { kind: 'personal_debt', id: 'd1' },
    label: 'Alex',
    currency: 'SGD',
    direction: 'outflow',
    date_basis: { kind: 'stated_due_date', value: '2026-10-01' },
    actual_payments: { count: 0, total: '0.00', entries: [] },
    cash_effect: 'bank_withdrawal',
    baseline_link: { available: false, category_key: null, relation: 'not_a_baseline_component', reason: 'x' },
    amount_basis: 'lump_sum_due',
    remaining_amount: { value: '500.00', currency: 'SGD' },
    remaining_unknown_reason: null,
    schedule: null,
    superseded_lump_sum: null,
    confidence: 'confirmed',
    confidence_basis: 'a recorded commitment states the future obligation',
    status: 'open',
    payment_status: 'unknown',
    stays_visible: true,
    overdue: false,
    basis: 'due date has not passed',
    ...over,
  }
}

function page(over: Record<string, unknown> = {}) {
  return {
    generation: 'g1',
    as_of: '2026-09-09',
    obligations: [],
    totals_by_currency: { SGD: { bank_withdrawal: '500.00', via_card_statement: '0.00', policy: 'p' } },
    baseline: { available: false, window: null, month_count: 0, reason: 'no window', basis: null },
    baseline_component_effects: [],
    rules: ['a remaining schedule replaces the lump-sum due; the superseded lump sum is shown but never counted'],
    ...over,
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('ObligationsView', () => {
  it('groups obligations by confidence and labels a likely receivable as income', async () => {
    loadObligations.mockResolvedValue(page({
      obligations: [
        obligation(),
        obligation({
          obligation_id: 'debt:d2', label: 'Jordan', direction: 'inflow',
          confidence: 'likely',
          confidence_basis: 'derived from recorded history only',
          amount_basis: 'lump_sum_due', remaining_amount: { value: '80.00', currency: 'SGD' },
        }),
      ],
    }))
    render(<ObligationsView />)

    expect(await screen.findByText(/Confirmed · 1 obligation/)).toBeInTheDocument()
    expect(screen.getByText(/Likely · 1 obligation/)).toBeInTheDocument()
    expect(screen.getByText('Likely income')).toBeInTheDocument()
    expect(screen.getByText('Confirmed')).toBeInTheDocument()
  })

  it('renders an unresolved cash-settlement requirement as a named visible gap', async () => {
    loadObligations.mockResolvedValue(page({
      obligations: [
        obligation({
          obligation_id: 'card_statement:s1:SGD', origin: 'card_statement', label: 'DBS Visa',
          amount_basis: 'unknown', remaining_amount: null,
          remaining_unknown_reason: 'the card evidence does not reach this unbilled window',
        }),
      ],
    }))
    render(<ObligationsView />)

    expect(await screen.findByText('Unresolved requirement')).toBeInTheDocument()
    expect(screen.getByText(/does not reach this unbilled window/)).toBeInTheDocument()
    expect(screen.getByText(/carries an unresolved cash-settlement requirement/)).toBeInTheDocument()
  })

  it('shows a superseded lump sum without counting it', async () => {
    loadObligations.mockResolvedValue(page({
      obligations: [
        obligation({
          obligation_id: 'installment:p1', origin: 'installment_plan', label: 'Sofa',
          amount_basis: 'remaining_schedule',
          remaining_amount: { value: '300.00', currency: 'SGD' },
          date_basis: { kind: 'scheduled_due_date', value: '2026-10-15' },
          superseded_lump_sum: { value: '450.00', currency: 'SGD', counted: false, reason: 'replaced' },
        }),
      ],
    }))
    render(<ObligationsView />)

    expect(await screen.findByText(/supersedes 450.00 SGD lump sum — not counted/)).toBeInTheDocument()
  })
})
