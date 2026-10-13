import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

const bill = {
  entry_anchor: 'grocer', effective_at: '2026-03-20T10:00:00', economic_direction: 'OUT', currency: 'SGD',
  amount: '51.25', open_amount: '51.25', description: 'Grocer',
}
const receivable = (debt_id: string, counterparty_label: string, extra: Record<string, unknown> = {}) => ({
  debt_id, direction: 'RECEIVABLE', currency: 'SGD', counterparty_label, opened_on: '2026-01-01', due_on: null, status: 'OPEN',
  closed_on: null, remaining_principal: { value: '0', currency: 'SGD' }, last_event: null, reconciliation_status: 'no_events', event_count: 0, ...extra,
})
const receivables = [
  receivable('d-ana', 'Ana'),
  receivable('d-closed', 'Closed Carl', { status: 'CLOSED' }),
  receivable('d-usd', 'Dollar Dee', { currency: 'USD' }),
  receivable('d-later', 'Later Lee', { opened_on: '2026-04-01' }),
]

const apiMock = vi.fn(async (path: string, _body?: unknown) => {
  if (path.startsWith('reporting/workspaces/debts/split-candidates')) return { generation: 'g1', candidates: [bill], next_cursor: null }
  if (path === 'categories') return { categories: [] }
  if (path === 'batch') return { status: 'applied' }
  throw new Error(`unmocked ${path}`)
})
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

const { SplitBillForm } = await import('./SplitBillForm')

afterEach(() => { cleanup(); apiMock.mockClear(); localStorage.clear() })

type Batch = { expected_generation: string; items: { kind: string; anchors: Record<string, string>; payload: Record<string, unknown> }[] }
const batchBody = () => apiMock.mock.calls.find(([path]) => path === 'batch')![1] as Batch

async function pickTheBill(onSaved = vi.fn()) {
  render(<SplitBillForm generation="g1" categories={['Food & Dining / Groceries', 'Unclassified / Unclassified']} receivables={receivables as never} onSaved={onSaved} onCancel={vi.fn()} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Split this' }))
  return onSaved
}

describe('SplitBillForm', () => {
  it('splits a bill between an existing debt, someone new and me in one batch', async () => {
    const onSaved = await pickTheBill()
    // Only open receivables in the bill's currency, opened by the bill's date.
    const person1 = screen.getByLabelText('Person 1') as HTMLSelectElement
    expect(within(person1).getAllByRole('option').map((o) => o.textContent)).toEqual(['Choose…', 'Ana', 'Someone new…'])

    fireEvent.change(person1, { target: { value: 'd-ana' } })
    fireEvent.change(screen.getByLabelText('Person 1 share'), { target: { value: '20' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add another person' }))
    fireEvent.change(screen.getByLabelText('Person 2'), { target: { value: 'new' } })
    fireEvent.change(screen.getByLabelText('Person 2 name'), { target: { value: 'Sam' } })
    fireEvent.change(screen.getByLabelText('Person 2 share'), { target: { value: '15.50' } })
    expect(screen.getByRole('status')).toHaveTextContent('Your share: SGD 15.75')

    // My share needs a category; Unclassified is not offered.
    fireEvent.click(screen.getByRole('button', { name: 'Save split' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Choose a category for your own share')
    const picker = screen.getByRole('combobox', { name: 'Category for your share' })
    fireEvent.focus(picker)
    expect(screen.queryByRole('button', { name: /Unclassified/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Food & Dining / Groceries' }))
    expect((screen.getByLabelText('Reason') as HTMLTextAreaElement).value).toBe('Split Grocer')

    fireEvent.click(screen.getByRole('button', { name: 'Save split' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
    const { expected_generation, items } = batchBody()
    expect(expected_generation).toBe('g1')
    expect(items.map((i) => i.kind)).toEqual(['PERSONAL_DEBT_EVENT_SET', 'PERSONAL_DEBT_OPEN', 'PERSONAL_DEBT_EVENT_SET', 'ENTRY_RESIDUAL_SET'])
    expect(items[0]).toMatchObject({ anchors: { debt: 'd-ana' }, payload: { event_kind: 'EXPENSE_PAID_FOR_OTHER', principal_amount: '20', effective_on: '2026-03-20', entry_allocations: [expect.objectContaining({ entry_anchor: 'grocer', principal_amount: '20' })] } })
    expect(items[1].payload).toMatchObject({ direction: 'RECEIVABLE', currency: 'SGD', counterparty_label: 'Sam', opened_on: '2026-03-20' })
    expect(items[2].anchors.debt).toBe(items[1].anchors.debt)
    expect(items[3]).toMatchObject({ anchors: { entry: 'grocer' }, payload: { expense_amount: '15.75', expense_category: 'Food & Dining / Groceries' } })
  })

  it('needs no category when the whole bill is owed back, and refuses shares above the bill', async () => {
    const onSaved = await pickTheBill()
    fireEvent.change(screen.getByLabelText('Person 1'), { target: { value: 'd-ana' } })
    fireEvent.change(screen.getByLabelText('Person 1 share'), { target: { value: '60.00' } })
    expect(screen.getByRole('status')).toHaveTextContent('SGD 8.75 more than the bill')
    fireEvent.click(screen.getByRole('button', { name: 'Save split' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('more than the SGD 51.25 left')
    fireEvent.change(screen.getByLabelText('Person 1 share'), { target: { value: '51.255' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save split' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('at most 2 decimals')
    expect(apiMock.mock.calls.some(([path]) => path === 'batch')).toBe(false)

    fireEvent.change(screen.getByLabelText('Person 1 share'), { target: { value: '51.25' } })
    expect(screen.getByRole('status')).toHaveTextContent('You kept no share')
    expect(screen.queryByRole('combobox', { name: 'Category for your share' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save split' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(batchBody().items.map((i) => i.kind)).toEqual(['PERSONAL_DEBT_EVENT_SET'])
  })

  it('refuses the same person twice', async () => {
    await pickTheBill()
    fireEvent.change(screen.getByLabelText('Person 1'), { target: { value: 'd-ana' } })
    fireEvent.change(screen.getByLabelText('Person 1 share'), { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add another person' }))
    fireEvent.change(screen.getByLabelText('Person 2'), { target: { value: 'd-ana' } })
    fireEvent.change(screen.getByLabelText('Person 2 share'), { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save split' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Each person appears once')
  })
})
