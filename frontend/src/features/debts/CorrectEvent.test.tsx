import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const money = (v: string) => ({ value: v, currency: 'SGD' })
type Item = { kind: string; anchors: Record<string, string>; payload: Record<string, unknown> }

const repaid = {
  event_id: 'e-repaid', event_kind: 'PRINCIPAL_REPAID', effective_on: '2026-01-03', opening_balance: false, signed: -1,
  principal: money('19.00'), expense_category: null, bound: true, dangling_reason: null, reason: 'Paid Alex back', journal_seq: 4,
}
const closedDetail = {
  generation: 'g1', as_of: '2026-02-01',
  debt: {
    debt_id: 'd-1', direction: 'PAYABLE', currency: 'SGD', counterparty_label: 'Alex', opened_on: '2026-01-01', due_on: null,
    status: 'CLOSED', closed_on: '2026-01-04', remaining_principal: money('0.00'), last_event: null,
    reconciliation_status: 'reconciled', event_count: 2, reason: 'Loan',
    principal_history: [
      { ...repaid, event_id: 'e-open', event_kind: 'BORROWED', effective_on: '2026-01-01', opening_balance: true, signed: 1, reason: 'Opening balance' },
      repaid,
    ],
  },
}
const eventRead = {
  generation: 'g1', debt_id: 'd-1',
  event: { event_id: 'e-repaid', event_kind: 'PRINCIPAL_REPAID', direction: 'PAYABLE', principal_amount: '19.00', effective_on: '2026-01-03',
    entry_allocations: [{ entry_anchor: 'book', principal_amount: '19.00', interest_amount: '0.00', fee_amount: '0.00', separate_fee_amount: '0.00' }],
    opening_balance: false, expense_category: null, reason: 'Paid Alex back' },
  sources: [{ entry_anchor: 'book', effective_at: '2026-01-03T10:00:00', economic_direction: 'OUT', currency: 'SGD', amount: '19.00', open_amount: '19.00', description: 'Bookshop' }],
}
const effect = {
  debt_id: 'd-1', counterparty_label: 'Alex', direction: 'PAYABLE', currency: 'SGD',
  before: { status: 'CLOSED', closed_on: '2026-01-04', remaining: '0.00' },
  after: { status: 'OPEN', closed_on: null, remaining: '19.00' },
  events: [{ event_id: 'e-open', event_kind: 'BORROWED', effective_on: '2026-01-01', signed: 1, principal: '19.00', balance: '19.00', change: null, balance_before: '19.00' }],
  removed: [{ event_id: 'e-repaid', event_kind: 'PRINCIPAL_REPAID', effective_on: '2026-01-03', signed: -1, principal: '19.00', balance: '0.00' }],
}

const apiMock = vi.fn(async (path: string, body?: unknown) => {
  if (path.startsWith('debts/d-1/events/e-repaid')) return eventRead
  if (path.startsWith('reporting/workspaces/debts/allocation-candidates')) return { generation: 'g1', debt_id: 'd-1', event_kind: 'PRINCIPAL_REPAID', required_direction: 'OUT', currency: 'SGD', next_cursor: null, candidates: [] }
  if (path === 'categories') return { categories: [] }
  if (path === 'debts/preview') return { generation: 'g1', items: (body as { items: Item[] }).items, debts: [effect] }
  if (path === 'batch') return { status: 'applied' }
  throw new Error(`unmocked ${path}`)
})
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

const { CorrectEventForm, RemoveEventForm } = await import('./CorrectEvent')

afterEach(() => { cleanup(); apiMock.mockClear() })

const drafted = () => (apiMock.mock.calls.filter(([path]) => path === 'debts/preview').at(-1)![1] as { items: Item[] }).items
const saved = () => (apiMock.mock.calls.find(([path]) => path === 'batch')![1] as { items: Item[] }).items

describe('RemoveEventForm', () => {
  it('reopens a closed debt around the removal, shows the later balances, then saves what was checked', async () => {
    const onSaved = vi.fn()
    render(<RemoveEventForm detail={closedDetail as never} event={repaid as never} generation="g1" onSaved={onSaved} onCancel={vi.fn()} />)
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Bookshop was not a repayment' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check removal' }))
    await screen.findByRole('button', { name: 'Remove event' })
    expect(drafted().map((i) => i.kind)).toEqual(['PERSONAL_DEBT_REOPEN', 'PERSONAL_DEBT_EVENT_RETIRE', 'PERSONAL_DEBT_CLOSE'])
    expect(drafted()[2].payload).toMatchObject({ effective_on: '2026-01-04' })

    // Leaving it open drops the reclose and asks for a fresh check.
    fireEvent.click(screen.getByRole('checkbox'))
    expect(screen.queryByRole('button', { name: 'Remove event' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Check removal' }))
    await screen.findByRole('button', { name: 'Remove event' })
    expect(drafted().map((i) => i.kind)).toEqual(['PERSONAL_DEBT_REOPEN', 'PERSONAL_DEBT_EVENT_RETIRE'])
    expect(screen.getByText(/closed → open/)).toBeInTheDocument()
    expect(screen.getByText('removed')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Remove event' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
    expect(saved()).toEqual(drafted())
  })

  it('needs a reason before it checks anything', async () => {
    render(<RemoveEventForm detail={closedDetail as never} event={repaid as never} generation="g1" onSaved={vi.fn()} onCancel={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Check removal' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('A reason is required')
    expect(apiMock.mock.calls.some(([path]) => path === 'debts/preview')).toBe(false)
  })
})

describe('CorrectEventForm', () => {
  it('starts from the recorded event and replaces it in one batch', async () => {
    render(<CorrectEventForm detail={closedDetail as never} event={repaid as never} generation="g1" categories={[]} onSaved={vi.fn()} onCancel={vi.fn()} />)
    expect(await screen.findByText(/Correct an event/)).toBeInTheDocument()
    expect(screen.getByLabelText('Event kind')).toHaveValue('PRINCIPAL_REPAID')
    expect(screen.getByLabelText('Effective date')).toHaveValue('2026-01-03')
    expect(screen.getByLabelText('Allocation 1 principal')).toHaveValue('19.00')
    expect(screen.getByLabelText('Event principal')).toHaveValue('')
    // The event's own claim is offered back to the picker.
    await waitFor(() => expect(apiMock.mock.calls.some(([path]) => String(path).includes('replacing=e-repaid'))).toBe(true))

    fireEvent.change(screen.getByLabelText('Effective date'), { target: { value: '2026-01-02' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Paid a day earlier' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check correction' }))
    await screen.findByRole('button', { name: 'Save correction' })
    const items = drafted()
    expect(items.map((i) => i.kind)).toEqual(['PERSONAL_DEBT_REOPEN', 'PERSONAL_DEBT_EVENT_RETIRE', 'PERSONAL_DEBT_EVENT_SET', 'PERSONAL_DEBT_CLOSE'])
    expect(items[1].anchors).toEqual({ debt: 'd-1', event: 'e-repaid' })
    expect(items[2].anchors.event).not.toBe('e-repaid')
    expect(items[2].payload).toMatchObject({ event_kind: 'PRINCIPAL_REPAID', effective_on: '2026-01-02', principal_amount: null,
      entry_allocations: eventRead.event.entry_allocations })
  })
})
