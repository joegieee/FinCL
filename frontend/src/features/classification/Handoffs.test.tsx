import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import type { Dashboard, Entry, Proposal } from '@/lib/api'

const apiMock = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

const { EntryDialog } = await import('./SingleTransactionEditor')

function entry(over: Partial<Entry> = {}): Entry {
  return {
    id: 'e1', anchor: 'a1', date: '2026-03-09', precision: 'DAY', amount: '-110.00', fee: '0',
    currency: 'SGD', account: 'Revolut', merchant: 'Repay Alex', description: 'TRANSFER TO ALEX',
    category: 'Unclassified / Unclassified', basis: 'Not categorized', source_location: 'Lines 1–2',
    bucket: 'spending', eligible: true, review: true, source_family: 'CSV', lifecycle_state: 'COMPLETED',
    cost_behavior: 'unresolved', cost_behavior_basis: 'category default', need: 'unresolved', need_basis: 'category default',
    role_review_state: 'unresolved', suggested_classification: '', linked: false, classification: null, debt_allocation: null,
    ...over,
  } as Entry
}

function dashboard(over: Partial<Dashboard> = {}): Dashboard {
  return {
    generation: 'g1', freshness: 'current', transfer_proposals: [],
    categories: ['Food & Dining / Groceries', 'Unclassified / Unclassified'],
    ...over,
  } as unknown as Dashboard
}

const debtCard = (over: Record<string, unknown> = {}) => ({
  debt_id: 'd-alex', direction: 'PAYABLE', currency: 'SGD', counterparty_label: 'Alex', opened_on: '2026-03-01',
  due_on: null, status: 'OPEN', closed_on: null, remaining_principal: { value: '200.00', currency: 'SGD' },
  last_event: null, reconciliation_status: 'reconciled', event_count: 1, ...over,
})

type Routes = Record<string, (body: unknown, path: string) => unknown>

/** Routes keyed by path prefix; the longest matching prefix answers. */
function mockRoutes(routes: Routes) {
  apiMock.mockImplementation((path: string, body?: unknown) => {
    if (path === 'categories') return Promise.resolve({ categories: [] })
    const key = Object.keys(routes).filter(prefix => path.startsWith(prefix)).sort((a, b) => b.length - a.length)[0]
    if (!key) return Promise.reject(new Error(`unmocked path ${path}`))
    try { return Promise.resolve(routes[key](body, path)) } catch (e) { return Promise.reject(e) }
  })
}

function calls(prefix: string): Array<[string, unknown]> {
  return apiMock.mock.calls.filter(([path]) => String(path).startsWith(prefix)) as Array<[string, unknown]>
}

afterEach(() => { cleanup(); vi.clearAllMocks(); localStorage.clear() })

type Item = { kind: string; anchors: Record<string, string>; payload: Record<string, unknown> }
/** The debt draft check: echoes the items, completing a blank principal as the server would. */
const draftCheck = (total: string) => (body: unknown) => ({
  generation: 'g1', debts: [],
  items: (body as { items: Item[] }).items.map(i => i.payload.principal_amount === null ? { ...i, payload: { ...i.payload, principal_amount: total } } : i),
})

describe('EntryDialog — "What is this?"', () => {
  it('asks what an outflow is before any classification field, defaulting to spending', () => {
    mockRoutes({})
    render(<EntryDialog entry={entry()} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    const choice = screen.getByRole('radiogroup', { name: 'What is this?' })
    expect(within(choice).getByRole('radio', { name: 'Spending' })).toHaveAttribute('aria-checked', 'true')
    expect(within(choice).getByRole('radio', { name: 'Transfer between my accounts' })).toBeInTheDocument()
    expect(within(choice).getByRole('radio', { name: 'Lending or repaying a debt' })).toBeInTheDocument()
    expect(screen.getByLabelText('Category')).toBeInTheDocument()
  })

  it('names the ordinary choice "Money in" for an inflow', () => {
    mockRoutes({})
    render(<EntryDialog entry={entry({ amount: '250.00', bucket: 'pending_review' })} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    expect(screen.getByRole('radio', { name: 'Money in' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: 'Borrowing, or a debt repaid to me' })).toBeInTheDocument()
  })
})

describe('EntryDialog — debt hand-off', () => {
  it('records the transaction against a debt, then re-reads it and stops asking to classify it', async () => {
    const onSaved = vi.fn()
    const onRelationshipSaved = vi.fn(async () => {})
    mockRoutes({
      // A GBP debt this SGD outflow cannot back, alongside the one it can.
      'reporting/workspaces/debts?': () => ({ generation: 'g1', as_of: '2026-03-31', i_owe: [debtCard(), debtCard({ debt_id: 'd-gbp', currency: 'GBP', counterparty_label: 'Sam' })], owed_to_me: [], next_cursor: null }),
      'reporting/workspaces/debts/allocation-candidates': () => ({
        generation: 'g1', debt_id: 'd-alex', event_kind: 'PRINCIPAL_REPAID', required_direction: 'OUT', currency: 'SGD', next_cursor: null,
        candidates: [{ entry_anchor: 'a1', effective_at: '2026-03-09 10:00:00', economic_direction: 'OUT', currency: 'SGD', amount: '110.00', open_amount: '110.00', description: 'TRANSFER TO ALEX' }],
      }),
      'reporting/workspaces/debts/d-alex': () => ({ generation: 'g1', as_of: '2026-03-31', debt: { ...debtCard(), principal_history: [], reason: 'Loan' } }),
      'debts/preview': draftCheck('110.00'),
      batch: () => ({ status: 'recorded' }),
      'transactions/entry': () => ({ generation: 'g2', entry: entry({ bucket: 'debt', debt_allocation: 'full', review: false }) }),
    })
    render(<EntryDialog entry={entry()} data={dashboard()} onClose={vi.fn()} onSaved={onSaved} onRelationshipSaved={onRelationshipSaved} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Lending or repaying a debt' }))
    const debtSelect = await screen.findByRole('combobox', { name: 'Debt' })
    // Only the same-currency debt is offered.
    expect(within(debtSelect).queryByText(/Sam/)).not.toBeInTheDocument()
    fireEvent.change(debtSelect, { target: { value: 'd-alex' } })

    // The debt's own event form opens with this transaction already allocated
    // for its unclaimed amount, offering only kinds an outflow can back.
    await screen.findByLabelText('Event principal')
    expect(screen.getByLabelText('Allocation 1 principal')).toHaveValue('110.00')
    const kinds = within(screen.getByLabelText('Event kind')).getAllByRole('option').map(o => o.textContent)
    expect(kinds).toEqual(['Repaid principal'])
    const [[candidatePath]] = calls('reporting/workspaces/debts/allocation-candidates')
    expect(candidatePath).toContain('anchor=a1')

    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Paid Alex back' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check event' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Save event' }))

    await screen.findByText('Recorded against the debt.')
    expect(screen.getByText(/fully explained by debt allocations/)).toBeInTheDocument()
    const [[, body]] = calls('batch')
    expect((body as { items: Item[] }).items).toMatchObject([{
      kind: 'PERSONAL_DEBT_EVENT_SET',
      payload: { event_kind: 'PRINCIPAL_REPAID', principal_amount: '110.00', entry_allocations: [expect.objectContaining({ entry_anchor: 'a1', principal_amount: '110.00' })] },
    }])
    // The refresh kept the dialog open (not the closing `onSaved`) ...
    expect(onRelationshipSaved).toHaveBeenCalledWith('recorded')
    expect(onSaved).not.toHaveBeenCalled()
    // ... and the re-read row is no longer offered for classification.
    expect(screen.queryByLabelText('Category')).not.toBeInTheDocument()
    expect(screen.queryByRole('radiogroup', { name: 'What is this?' })).not.toBeInTheDocument()
  })

  it('returns to the classification fields when the debt only explains part of the amount', async () => {
    mockRoutes({
      'reporting/workspaces/debts?': () => ({ generation: 'g1', as_of: '2026-03-31', i_owe: [debtCard()], owed_to_me: [], next_cursor: null }),
      'reporting/workspaces/debts/allocation-candidates': () => ({
        generation: 'g1', debt_id: 'd-alex', event_kind: 'PRINCIPAL_REPAID', required_direction: 'OUT', currency: 'SGD', next_cursor: null,
        candidates: [{ entry_anchor: 'a1', effective_at: '2026-03-09 10:00:00', economic_direction: 'OUT', currency: 'SGD', amount: '110.00', open_amount: '110.00', description: 'TRANSFER TO ALEX' }],
      }),
      'reporting/workspaces/debts/d-alex': () => ({ generation: 'g1', as_of: '2026-03-31', debt: { ...debtCard(), principal_history: [], reason: 'Loan' } }),
      'debts/preview': draftCheck('60.00'),
      batch: () => ({ status: 'recorded' }),
      'transactions/entry': () => ({ generation: 'g2', entry: entry({ debt_allocation: 'partial' }) }),
    })
    render(<EntryDialog entry={entry()} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} onRelationshipSaved={async () => {}} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Lending or repaying a debt' }))
    fireEvent.change(await screen.findByRole('combobox', { name: 'Debt' }), { target: { value: 'd-alex' } })
    fireEvent.change(await screen.findByLabelText('Allocation 1 principal'), { target: { value: '60.00' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Part of this repaid Alex' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check event' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Save event' }))
    await screen.findByText(/classify the rest below/)
    expect(screen.getByRole('radio', { name: 'Spending' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByLabelText('Category')).toBeInTheDocument()
  })

  it('says so when the transaction cannot back an event on the chosen debt', async () => {
    mockRoutes({
      'reporting/workspaces/debts?': () => ({ generation: 'g1', as_of: '2026-03-31', i_owe: [debtCard()], owed_to_me: [], next_cursor: null }),
      'reporting/workspaces/debts/allocation-candidates': () => ({ generation: 'g1', debt_id: 'd-alex', event_kind: 'PRINCIPAL_REPAID', required_direction: 'OUT', currency: 'SGD', next_cursor: null, candidates: [] }),
      'reporting/workspaces/debts/d-alex': () => ({ generation: 'g1', as_of: '2026-03-31', debt: { ...debtCard(), principal_history: [], reason: 'Loan' } }),
    })
    render(<EntryDialog entry={entry()} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Lending or repaying a debt' }))
    fireEvent.change(await screen.findByRole('combobox', { name: 'Debt' }), { target: { value: 'd-alex' } })
    expect(await screen.findByText(/already fully allocated elsewhere/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Event principal')).not.toBeInTheDocument()
  })
})

describe('EntryDialog — transfer hand-off', () => {
  const proposal = {
    id: 'p1', link_kind: 'SAME_CURRENCY', method: 'auto', left: 'a1', right: 'b9',
    left_account: 'Revolut', right_account: 'DBS', left_description: 'TO DBS', right_description: 'FROM REVOLUT',
    left_amount: '-110.00', left_currency: 'SGD', right_amount: '110.00', right_currency: 'SGD',
    left_date: '2026-03-09', right_date: '2026-03-09', basis: 'Same amount and day',
  } as unknown as Proposal

  it('confirms a proposed transfer involving this transaction', async () => {
    mockRoutes({
      preview: () => ({ status: 'previewed' }),
      commands: () => ({ status: 'recorded' }),
      'transactions/entry': () => ({ generation: 'g2', entry: entry({ bucket: 'internal', linked: true, category: 'Internal / Transfer' }) }),
    })
    render(<EntryDialog entry={entry()} data={dashboard({ transfer_proposals: [proposal] })} onClose={vi.fn()} onSaved={vi.fn()} onRelationshipSaved={async () => {}} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Transfer between my accounts' }))
    // The *other* leg is what the user needs to recognise.
    expect(screen.getByText(/DBS/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm transfer' }))
    fireEvent.change(screen.getByPlaceholderText(/same transfer/), { target: { value: 'Moved savings' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check decision' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Save decision' }))
    await screen.findByText('Recorded as a transfer.')
    expect(screen.getByText(/linked relationship owns this movement/)).toBeInTheDocument()
    const [[, body]] = calls('commands')
    expect(body).toMatchObject({ kind: 'TRANSFER_DECISION', anchors: { left: 'a1', right: 'b9' }, payload: { decision: 'confirm', reason: 'Moved savings' } })
  })

  it('offers an inflow with no match the self-transfer incoming type instead', () => {
    mockRoutes({})
    render(<EntryDialog entry={entry({ amount: '250.00', bucket: 'pending_review' })} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Transfer between my accounts' }))
    fireEvent.click(screen.getByRole('button', { name: /account I don't import/ }))
    expect(screen.getByRole('radio', { name: 'Money in' })).toHaveAttribute('aria-checked', 'true')
    expect((screen.getByLabelText('Incoming type') as HTMLSelectElement).value).toBe('self_transfer')
  })

  it('does not pretend an outflow with no match can be linked here', () => {
    mockRoutes({})
    render(<EntryDialog entry={entry()} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.click(screen.getByRole('radio', { name: 'Transfer between my accounts' }))
    expect(screen.getByText(/by hand is not available here yet/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /account I don't import/ })).not.toBeInTheDocument()
  })
})

describe('EntryDialog — installment link', () => {
  const plan = { plan_id: 'pl1', card_account_id: 'card', currency: 'SGD', plan_reference: null, merchant_label: 'Laptop', opened_on: '2026-03-01', status: 'CONFIRMED', effective_status: 'ACTIVE', interpretation_status: 'ok', bound: true, dangling_reason: null, progress: {} }

  it('links a purchase to a plan alongside, not instead of, its classification', async () => {
    mockRoutes({
      'reporting/workspaces/installments?': () => ({ generation: 'g1', plans: [plan], next_cursor: null }),
      'reporting/workspaces/installments/entry-candidates': () => ({
        generation: 'g1', plan_id: 'pl1', purpose: 'purchase', currency: 'SGD', card_account_id: 'card', next_cursor: null,
        candidates: [{ entry_anchor: 'a1', effective_at: '2026-03-09 10:00:00', account_id: 'card', currency: 'SGD', amount: '110.00', open_amount: '110.00', open_fee: '0.00', description: 'LAPTOP STORE' }],
      }),
      'reporting/workspaces/installments/pl1': () => ({ generation: 'g1', as_of: '2026-03-31', plan, source_occurrences: [], purchase_allocations: [], schedule: [], settlements: [], views: {} }),
    })
    render(<EntryDialog entry={entry()} data={dashboard()} onClose={vi.fn()} onSaved={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Part of an installment plan?' }))
    fireEvent.change(await screen.findByRole('combobox', { name: 'Installment plan' }), { target: { value: 'pl1' } })
    expect(await screen.findByText(/Attribute the original purchase/)).toBeInTheDocument()
    await waitFor(() => expect(calls('reporting/workspaces/installments/entry-candidates')[0][0]).toContain('anchor=a1'))
    expect(screen.getAllByText(/LAPTOP STORE/).length).toBeGreaterThan(0)
    // Still spending: the classification fields stay available.
    expect(screen.getByLabelText('Category')).toBeInTheDocument()
  })
})
