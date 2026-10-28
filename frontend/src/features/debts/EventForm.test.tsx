import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const money = (v: string) => ({ value: v, currency: 'SGD' })

const receivableDetail = {
  generation: 'sha256:abc',
  as_of: '2026-03-31',
  debt: {
    debt_id: 'd-2', direction: 'RECEIVABLE', currency: 'SGD', counterparty_label: 'Jo',
    opened_on: '2026-03-01', due_on: null, status: 'OPEN', closed_on: null,
    remaining_principal: money('0'), last_event: null,
    reconciliation_status: 'no_events', event_count: 0,
    reason: 'Jo owes me', principal_history: [],
  },
}

const candidates = {
  generation: 'sha256:abc', debt_id: 'd-2', event_kind: 'EXPENSE_PAID_FOR_OTHER',
  required_direction: 'OUT', currency: 'SGD', next_cursor: null,
  candidates: [
    { entry_anchor: 'anchor-120', effective_at: '2026-03-05T10:00:00', economic_direction: 'OUT',
      currency: 'SGD', amount: '120.00', open_amount: '120.00', description: 'Flat rent March' },
  ],
}

type Item = { kind: string; anchors: Record<string, string>; payload: Record<string, unknown> }
const calls: Array<{ path: string; body?: unknown }> = []
// What the server makes of a principal left blank.
let sourcesTotal = '120.00'
const apiMock = vi.fn(async (path: string, body?: unknown) => {
  calls.push({ path, body })
  if (path.startsWith('reporting/workspaces/debts/allocation-candidates')) return candidates
  if (path === 'categories') return { categories: [{ key: 'Social / Retired Club', archived: true }] }
  if (path === 'debts/preview') {
    const items = (body as { items: Item[] }).items.map((i) => i.payload.principal_amount === null
      ? { ...i, payload: { ...i.payload, principal_amount: sourcesTotal } } : i)
    return {
      generation: 'sha256:abc', items,
      debts: [{ debt_id: 'd-2', counterparty_label: 'Jo', direction: 'RECEIVABLE', currency: 'SGD',
        before: { status: 'OPEN', closed_on: null, remaining: '0' }, after: { status: 'OPEN', closed_on: null, remaining: '500.00' },
        events: [{ event_id: 'new', event_kind: 'LENT', effective_on: '2026-03-01', signed: 1, principal: '500.00', balance: '500.00', change: 'added', balance_before: null }],
        removed: [] }],
    }
  }
  if (path === 'batch') return { status: 'applied' }
  throw new Error(`unmocked ${path}`)
})
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

afterEach(() => { cleanup(); apiMock.mockClear(); calls.length = 0; sourcesTotal = '120.00' })

const payableDetail = {
  ...receivableDetail,
  debt: { ...receivableDetail.debt, debt_id: 'd-3', direction: 'PAYABLE', reason: 'I owe Jo' },
}

const ledgerCategories = [
  'Food & Dining / Dining Out', 'Housing / Rent', 'Internal / Transfer',
  'Unclassified / Unclassified', 'Social / Retired Club',
]

async function mount(detail: typeof receivableDetail = receivableDetail) {
  const { EventForm } = await import('./EventForm')
  return render(
    <EventForm detail={detail as never} generation="sha256:abc" categories={ledgerCategories} onSaved={vi.fn()} onCancel={vi.fn()} />,
  )
}

const saved = () => (calls.find((c) => c.path === 'batch')!.body as { items: Item[] }).items[0].payload

async function checkThenSave() {
  fireEvent.click(screen.getByText('Check event'))
  fireEvent.click(await screen.findByText('Save event'))
  await waitFor(() => expect(calls.some((c) => c.path === 'batch')).toBe(true))
}

describe('EventForm', () => {
  it('records an opening balance with no source allocations', async () => {
    await mount()
    fireEvent.change(screen.getByLabelText('Event kind'), { target: { value: 'LENT' } })
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.change(screen.getByLabelText('Event principal'), { target: { value: '500.00' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Opening balance lent to Jo' } })
    fireEvent.click(screen.getByText('Check event'))

    // The check shows the balance after the event before anything is saved.
    expect(await screen.findByText('new')).toBeInTheDocument()
    expect(calls.some((c) => c.path === 'batch')).toBe(false)
    fireEvent.click(screen.getByText('Save event'))
    await waitFor(() => expect(calls.some((c) => c.path === 'batch')).toBe(true))
    expect(saved()).toMatchObject({ event_kind: 'LENT', opening_balance: true, principal_amount: '500.00', entry_allocations: [] })
  })

  it('records a forgiveness with no allocations and no category', async () => {
    await mount()
    fireEvent.change(screen.getByLabelText('Event kind'), { target: { value: 'RECEIVABLE_WRITTEN_OFF' } })
    fireEvent.change(screen.getByLabelText('Event principal'), { target: { value: '70.00' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Jo will not repay' } })
    await checkThenSave()
    expect(saved()).toMatchObject({ event_kind: 'RECEIVABLE_WRITTEN_OFF', entry_allocations: [], expense_category: null })
  })

  it('leaves the principal of a source-linked event to the server and shows its total', async () => {
    await mount({ ...receivableDetail, debt: { ...receivableDetail.debt, opened_on: '2026-01-01' } })
    fireEvent.change(screen.getByLabelText('Event kind'), { target: { value: 'EXPENSE_PAID_FOR_OTHER' } })
    await screen.findByLabelText('Allocation 1 source')
    fireEvent.change(screen.getByLabelText('Allocation 1 source'), { target: { value: 'rent' } })
    fireEvent.click(await screen.findByText('Flat rent March'))
    expect(screen.getByLabelText('Effective date')).toHaveValue('2026-03-05')
    fireEvent.change(screen.getByLabelText('Allocation 1 principal'), { target: { value: '45.50' } })
    expect(screen.getByLabelText('Event principal')).toHaveValue('')
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: "Jo's share of the flat" } })

    sourcesTotal = '45.50'
    await checkThenSave()
    const draft = (calls.find((c) => c.path === 'debts/preview')!.body as { items: Item[] }).items[0].payload
    expect(draft.principal_amount).toBeNull()
    expect(saved()).toMatchObject({
      principal_amount: '45.50',
      entry_allocations: [{ entry_anchor: 'anchor-120', principal_amount: '45.50', interest_amount: '0.00', fee_amount: '0.00', separate_fee_amount: '0.00' }],
    })
  })

  it('folds interest and fee columns away until asked for', async () => {
    await mount()
    fireEvent.change(screen.getByLabelText('Event kind'), { target: { value: 'EXPENSE_PAID_FOR_OTHER' } })
    await screen.findByLabelText('Allocation 1 source')
    expect(screen.queryByLabelText('Allocation 1 interest')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Interest & fees' }))
    expect(screen.getByLabelText('Allocation 1 interest')).toBeInTheDocument()
  })

  it('needs a source for a source-linked event and drops a stale check on any edit', async () => {
    await mount()
    fireEvent.change(screen.getByLabelText('Event kind'), { target: { value: 'EXPENSE_PAID_FOR_OTHER' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: "Jo's share of the flat" } })
    await screen.findByLabelText('Allocation 1 source')
    fireEvent.click(screen.getByText('Check event'))
    await screen.findByText(/Pick the transaction/)
    expect(calls.some((c) => c.path === 'debts/preview')).toBe(false)

    fireEvent.change(screen.getByLabelText('Allocation 1 source'), { target: { value: 'rent' } })
    fireEvent.click(await screen.findByText('Flat rent March'))
    fireEvent.click(screen.getByText('Check event'))
    await screen.findByText('Save event')
    fireEvent.change(screen.getByLabelText('Allocation 1 principal'), { target: { value: '80.00' } })
    expect(screen.queryByText('Save event')).not.toBeInTheDocument()
    expect(screen.getByText('Check event')).toBeInTheDocument()
  })

  it('chooses an expense paid for me from the shared category list', async () => {
    await mount(payableDetail)
    fireEvent.change(screen.getByLabelText('Event kind'), { target: { value: 'EXPENSE_PAID_FOR_ME' } })
    fireEvent.change(screen.getByLabelText('Event principal'), { target: { value: '25.00' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Jo covered dinner' } })

    // Free text that is not a category is not saved as one.
    const picker = screen.getByRole('combobox', { name: 'Expense category' })
    fireEvent.change(picker, { target: { value: 'Dining' } })
    fireEvent.click(screen.getByText('Check event'))
    await screen.findByText(/an expense category from the list/)
    expect(calls.some((c) => c.path === 'debts/preview')).toBe(false)

    fireEvent.change(picker, { target: { value: '' } })
    await waitFor(() => expect(screen.queryByRole('option', { name: /Retired Club/ })).not.toBeInTheDocument())
    const offered = screen.getAllByRole('option').map((o) => o.textContent)
    expect(offered).toEqual(expect.arrayContaining(['Food & Dining / Dining Out', 'Housing / Rent']))
    expect(offered.some((o) => /Internal|Unclassified/.test(o ?? ''))).toBe(false)

    fireEvent.change(picker, { target: { value: 'dining' } })
    fireEvent.click(screen.getByRole('button', { name: 'Food & Dining / Dining Out' }))
    await checkThenSave()
    expect(saved()).toMatchObject({ event_kind: 'EXPENSE_PAID_FOR_ME', expense_category: 'Food & Dining / Dining Out', entry_allocations: [] })
  })

  it('clears an own-account transfer type on a borrowed inflow in the same change, and any other type only when asked', async () => {
    const inflow = { entry_anchor: 'in-500', effective_at: '2026-03-02T09:00:00', economic_direction: 'IN', currency: 'SGD',
      amount: '500.00', open_amount: '500.00', description: 'From Jo', incoming_type: 'self_transfer' }
    const { EventForm } = await import('./EventForm')
    const first = render(<EventForm detail={payableDetail as never} generation="sha256:abc" seed={inflow} onSaved={vi.fn()} onCancel={vi.fn()} />)
    const forced = screen.getByRole('checkbox', { name: /Clear its incoming type “Own-account transfer”/ })
    expect(forced).toBeChecked()
    expect(forced).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Jo lent me this' } })
    sourcesTotal = '500.00'
    fireEvent.click(screen.getByText('Check event'))
    await screen.findByText('Save event')
    const items = (calls.find((c) => c.path === 'debts/preview')!.body as { items: Item[] }).items
    expect(items.map((i) => i.kind)).toEqual(['INBOUND_CLASSIFICATION_RETIRE', 'PERSONAL_DEBT_EVENT_SET'])
    expect(items[0]).toMatchObject({ anchors: { entry: 'in-500' }, payload: { reason: 'Jo lent me this' } })
    first.unmount(); calls.length = 0

    render(<EventForm detail={payableDetail as never} generation="sha256:abc" seed={{ ...inflow, incoming_type: 'income' }} onSaved={vi.fn()} onCancel={vi.fn()} />)
    const optional = screen.getByRole('checkbox', { name: /Clear its incoming type “Income”/ })
    expect(optional).not.toBeChecked()
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Jo lent me this' } })
    fireEvent.click(screen.getByText('Check event'))
    await screen.findByText('Save event')
    expect((calls.find((c) => c.path === 'debts/preview')!.body as { items: Item[] }).items.map((i) => i.kind)).toEqual(['PERSONAL_DEBT_EVENT_SET'])
  })
})
