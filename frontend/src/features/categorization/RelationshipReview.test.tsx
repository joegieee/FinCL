import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

const apiMock = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

const { CategorizationView } = await import('./CategorizationView')

afterEach(() => { cleanup(); vi.clearAllMocks(); localStorage.clear() })

const summary = { counts: {}, active_categorization_denominator: 0, progress_numerator: 0, needs_attention: 0 }
const focusOf = (path: string) => new URLSearchParams(path.split('?')[1]).get('focus')

const debtCard = {
  candidate_id: 'cd1', kind: 'description', label: 'ALEX TAN', transaction_count: 2, active_count: 2,
  date_range: { first: '2026-03-10', last: '2026-03-15' }, accounts: ['acct'], directions: { IN: 1, OUT: 1 }, examples: ['ALEX TAN'],
  suggested_debts: [{ debt_id: 'd-alex', counterparty_label: 'Alex', direction: 'RECEIVABLE', currency: 'SGD', earlier: 1 }],
  relationship_basis: 'You recorded 1 earlier transaction with this payee against Alex.',
}
const debtRecords = [
  { anchor: 'r-out', account: 'acct', currency: 'SGD', amount: '-30.00', date: '2026-03-10', direction: 'OUT', active: true, description: 'ALEX TAN' },
  { anchor: 'r-in', account: 'acct', currency: 'SGD', amount: '20.00', date: '2026-03-15', direction: 'IN', active: true, description: 'ALEX TAN' },
]
const alex = {
  debt_id: 'd-alex', direction: 'RECEIVABLE', currency: 'SGD', counterparty_label: 'Alex', opened_on: '2026-03-01', due_on: null,
  status: 'OPEN', closed_on: null, remaining_principal: { value: '50.00', currency: 'SGD' }, last_event: null, reconciliation_status: 'reconciled', event_count: 1,
}

describe('CategorizationView — relationship discovery focuses', () => {
  it('proposes a debt movement with its evidence and records one transaction against the suggested debt', async () => {
    const onSaved = vi.fn(async () => {})
    let saved = false
    apiMock.mockImplementation((path: string, body?: unknown) => {
      // The save moves the ledger to g2; a read still pinned to g1 afterwards
      // is refused exactly as the server refuses it.
      if (saved && path.includes('generation=g1')) return Promise.reject(new Error('STALE_GENERATION: reload categorization'))
      if (path === 'overview') return Promise.resolve({ generation: saved ? 'g2' : 'g1' })
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve(summary)
      if (path.startsWith('reporting/workspaces/categorization/rule-suggestions')) return Promise.resolve({ suggestions: [] })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) {
        const focus = focusOf(path)
        return Promise.resolve(focus === 'debts' ? { total: 1, next_cursor: null, candidates: [debtCard] } : { total: 0, next_cursor: null, candidates: [] })
      }
      if (path.startsWith('reporting/workspaces/categorization/candidates/cd1')) return Promise.resolve({ candidate: debtCard, records: debtRecords })
      if (path.startsWith('reporting/workspaces/debts/allocation-candidates')) return Promise.resolve({
        generation: 'g1', debt_id: 'd-alex', event_kind: 'LENT', required_direction: 'OUT', currency: 'SGD', next_cursor: null,
        candidates: [{ entry_anchor: 'r-out', effective_at: '2026-03-10 10:00:00', economic_direction: 'OUT', currency: 'SGD', amount: '30.00', open_amount: '30.00', description: 'ALEX TAN' }],
      })
      if (path.startsWith('reporting/workspaces/debts/d-alex')) return Promise.resolve({ generation: 'g1', as_of: '2026-03-31', debt: { ...alex, principal_history: [], reason: 'Loan' } })
      if (path.startsWith('reporting/workspaces/debts?')) return Promise.resolve({ generation: 'g1', as_of: '2026-03-31', i_owe: [], owed_to_me: [alex], next_cursor: null })
      if (path === 'debts/preview') {
        const items = (body as { items: Array<{ payload: Record<string, unknown> }> }).items
        return Promise.resolve({ generation: 'g1', debts: [], items: items.map(i => ({ ...i, payload: { ...i.payload, principal_amount: '30.00' } })) })
      }
      if (path === 'batch') { saved = true; return Promise.resolve({ status: 'recorded' }) }
      return Promise.reject(new Error(`unexpected ${path} ${JSON.stringify(body)}`))
    })
    // The parent's refresh does not re-render this view with g2 in time: the
    // reload after the save must find the new generation by itself.
    render(<CategorizationView generation="g1" freshness="current" categories={[]} onSaved={onSaved} />)
    fireEvent.click(await screen.findByRole('tab', { name: /Looks like a debt movement/ }))
    fireEvent.click(await screen.findByRole('button', { name: /ALEX TAN/ }))

    const panel = await screen.findByRole('region', { name: 'Review ALEX TAN' })
    expect(within(panel).getByText(/You recorded 1 earlier transaction with this payee against Alex/)).toBeInTheDocument()
    // Nothing to set for the whole group: one action per movement.
    expect(within(panel).queryByRole('combobox', { name: 'Category' })).not.toBeInTheDocument()
    const actions = within(panel).getAllByRole('button', { name: 'Record against a debt' })
    expect(actions).toHaveLength(2)
    fireEvent.click(actions[0])

    // The suggested debt is pre-selected and the debt's own form is seeded.
    await waitFor(() => expect((within(panel).getByLabelText('Debt') as HTMLSelectElement).value).toBe('d-alex'))
    expect(((await within(panel).findByLabelText('Allocation 1 principal')) as HTMLInputElement).value).toBe('30.00')
    fireEvent.change(within(panel).getByLabelText('Reason'), { target: { value: 'Lent Alex another 30' } })
    fireEvent.click(within(panel).getByRole('button', { name: 'Check event' }))
    fireEvent.click(await within(panel).findByRole('button', { name: 'Save event' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))
    await waitFor(() => expect(apiMock.mock.calls.some(([path]) => String(path).startsWith('reporting/workspaces/categorization/candidates?') && String(path).includes('generation=g2'))).toBe(true))
    expect(screen.queryByText(/STALE_GENERATION/)).not.toBeInTheDocument()
    const [, body] = apiMock.mock.calls.find(([path]) => path === 'batch')!
    expect((body as { items: unknown[] }).items).toMatchObject([{ kind: 'PERSONAL_DEBT_EVENT_SET', anchors: { debt: 'd-alex' }, payload: { principal_amount: '30.00', entry_allocations: [expect.objectContaining({ entry_anchor: 'r-out' })] } }])
  })

  it('opens the installment plan confirmation for a proposed chain', async () => {
    const chainCard = {
      candidate_id: 'ci1', kind: 'installment_chain', label: 'Split Payment · 6 months', transaction_count: 2, active_count: 2,
      date_range: { first: '2026-06-21', last: '2026-07-21' }, accounts: ['card'], directions: { OUT: 2 }, examples: [],
      installment_candidate_key: 'card:Split Payment:6:a5', relationship_basis: 'The statement prints these as instalments 2–3 of 6; 2 are on your statements.',
    }
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve(summary)
      if (path.startsWith('reporting/workspaces/categorization/rule-suggestions')) return Promise.resolve({ suggestions: [] })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve(focusOf(path) === 'installments' ? { total: 1, next_cursor: null, candidates: [chainCard] } : { total: 0, next_cursor: null, candidates: [] })
      if (path.startsWith('reporting/workspaces/categorization/candidates/ci1')) return Promise.resolve({ candidate: chainCard, records: [
        { anchor: 'i2', account: 'card', currency: 'SGD', amount: '-15.00', date: '2026-06-21', direction: 'OUT', active: true, description: 'Split Payment 2/6 Instalment' },
      ] })
      if (path.startsWith('reporting/workspaces/installments/candidates')) return Promise.resolve({ generation: 'g1', ambiguous_groups: [], claimed_occurrence_count: 0, candidates: [{
        candidate_key: 'card:Split Payment:6:a5', card_account_id: 'card', currency: 'SGD', family: 'Split Payment', term_months: 6,
        merchant_label: 'Split Payment · 6 months', opened_on: '2026-06-21', first_ordinal: 2, last_ordinal: 3, observed_count: 2, complete: false,
        observed_total: { value: '30.00', currency: 'SGD' }, known_principal: null, occurrences: [], source: 'mari-card-installment-block-v1',
      }] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={[]} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('tab', { name: /Installment candidates/ }))
    fireEvent.click(await screen.findByRole('button', { name: /Split Payment · 6 months/ }))
    const panel = await screen.findByRole('region', { name: /Review Split Payment/ })
    expect(within(panel).getByText(/instalments 2–3 of 6/)).toBeInTheDocument()
    fireEvent.click(await within(panel).findByRole('button', { name: 'Confirm this plan' }))
    expect(await within(panel).findByText(/Confirm a split payment/)).toBeInTheDocument()
  })

  it('explains where debt proposals come from when there are none', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path.startsWith('reporting/workspaces/categorization/summary')) return Promise.resolve(summary)
      if (path.startsWith('reporting/workspaces/categorization/rule-suggestions')) return Promise.resolve({ suggestions: [] })
      if (path.startsWith('reporting/workspaces/categorization/candidates?')) return Promise.resolve({ total: 0, next_cursor: null, candidates: [] })
      return Promise.reject(new Error(`unexpected ${path}`))
    })
    render(<CategorizationView generation="g1" freshness="current" categories={[]} onSaved={() => {}} />)
    fireEvent.click(await screen.findByRole('tab', { name: /Looks like a debt movement/ }))
    expect(await screen.findByText(/once you have recorded an earlier one with the same payee/)).toBeInTheDocument()
  })
})
