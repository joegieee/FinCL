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

const calls: Array<{ path: string; body?: unknown }> = []
const apiMock = vi.fn(async (path: string, body?: unknown) => {
  calls.push({ path, body })
  if (path.startsWith('debts/allocation-candidates')) return candidates
  if (path === 'categories') return { categories: [{ key: 'Social / Retired Club', archived: true }] }
  if (path === 'preview') return { valid: true }
  if (path === 'commands') return { status: 'applied' }
  throw new Error(`unmocked ${path}`)
})
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

afterEach(() => { cleanup(); apiMock.mockClear(); calls.length = 0 })

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

describe('EventForm', () => {
  it('records an opening balance with no source allocations', async () => {
    await mount()
    fireEvent.change(screen.getByLabelText('Event kind'), { target: { value: 'LENT' } })
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.change(screen.getByLabelText('Event principal'), { target: { value: '500.00' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Opening balance lent to Jo' } })
    fireEvent.click(screen.getByText('Preview and save'))

    await waitFor(() => expect(calls.some((c) => c.path === 'commands')).toBe(true))
    const commit = calls.find((c) => c.path === 'commands')!.body as { payload: Record<string, unknown> }
    expect(commit.payload.event_kind).toBe('LENT')
    expect(commit.payload.opening_balance).toBe(true)
    expect(commit.payload.entry_allocations).toEqual([])
  })

  it('records a forgiveness with no allocations and no category', async () => {
    await mount()
    fireEvent.change(screen.getByLabelText('Event kind'), { target: { value: 'RECEIVABLE_WRITTEN_OFF' } })
    fireEvent.change(screen.getByLabelText('Event principal'), { target: { value: '70.00' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Jo will not repay' } })
    fireEvent.click(screen.getByText('Preview and save'))

    await waitFor(() => expect(calls.some((c) => c.path === 'commands')).toBe(true))
    const commit = calls.find((c) => c.path === 'commands')!.body as { payload: Record<string, unknown> }
    expect(commit.payload.event_kind).toBe('RECEIVABLE_WRITTEN_OFF')
    expect(commit.payload.entry_allocations).toEqual([])
    expect(commit.payload.expense_category).toBeNull()
  })

  it('seeds an empty event principal from the first source picked', async () => {
    await mount()
    fireEvent.change(screen.getByLabelText('Event kind'), { target: { value: 'EXPENSE_PAID_FOR_OTHER' } })

    await screen.findByLabelText('Allocation 1 source')
    fireEvent.change(screen.getByLabelText('Allocation 1 source'), { target: { value: 'rent' } })
    fireEvent.click(await screen.findByText('Flat rent March'))

    await waitFor(() => expect(screen.getByLabelText('Event principal')).toHaveValue('120.00'))
    expect(screen.queryByText(/must match the event principal/)).not.toBeInTheDocument()
  })

  it('keeps the event principal on the allocated total and the date on the source', async () => {
    await mount({ ...receivableDetail, debt: { ...receivableDetail.debt, opened_on: '2026-01-01' } })
    fireEvent.change(screen.getByLabelText('Event kind'), { target: { value: 'EXPENSE_PAID_FOR_OTHER' } })

    await screen.findByLabelText('Allocation 1 source')
    fireEvent.change(screen.getByLabelText('Allocation 1 source'), { target: { value: 'rent' } })
    fireEvent.click(await screen.findByText('Flat rent March'))
    await waitFor(() => expect(screen.getByLabelText('Event principal')).toHaveValue('120.00'))
    expect(screen.getByLabelText('Effective date')).toHaveValue('2026-03-05')

    fireEvent.change(screen.getByLabelText('Allocation 1 principal'), { target: { value: '45.50' } })
    expect(screen.getByLabelText('Event principal')).toHaveValue('45.50')
    expect(screen.queryByText(/must match the event principal/)).not.toBeInTheDocument()
  })

  it('folds interest and fee columns away until asked for', async () => {
    await mount()
    fireEvent.change(screen.getByLabelText('Event kind'), { target: { value: 'EXPENSE_PAID_FOR_OTHER' } })
    await screen.findByLabelText('Allocation 1 source')
    expect(screen.queryByLabelText('Allocation 1 interest')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Interest & fees' }))
    expect(screen.getByLabelText('Allocation 1 interest')).toBeInTheDocument()
  })

  it('leaves a principal the user already typed alone', async () => {
    await mount()
    fireEvent.change(screen.getByLabelText('Event kind'), { target: { value: 'EXPENSE_PAID_FOR_OTHER' } })
    fireEvent.change(screen.getByLabelText('Event principal'), { target: { value: '80.00' } })

    await screen.findByLabelText('Allocation 1 source')
    fireEvent.change(screen.getByLabelText('Allocation 1 source'), { target: { value: 'rent' } })
    fireEvent.click(await screen.findByText('Flat rent March'))

    await screen.findByText(/must match the event principal/)
    expect(screen.getByLabelText('Event principal')).toHaveValue('80.00')

    fireEvent.click(screen.getByRole('button', { name: /Use SGD/ }))
    expect(screen.getByLabelText('Event principal')).toHaveValue('120.00')
  })

  it('requires the source allocations to sum to the event principal', async () => {
    await mount()
    fireEvent.change(screen.getByLabelText('Event kind'), { target: { value: 'EXPENSE_PAID_FOR_OTHER' } })
    fireEvent.change(screen.getByLabelText('Event principal'), { target: { value: '80.00' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: "Jo's share of the flat" } })

    await screen.findByLabelText('Allocation 1 source')  // candidate list loaded
    fireEvent.change(screen.getByLabelText('Allocation 1 source'), { target: { value: 'rent' } })
    fireEvent.click(await screen.findByText('Flat rent March'))

    // principal field still 0.00 -> unbalanced -> blocked
    fireEvent.click(screen.getByText('Preview and save'))
    await screen.findByText(/must sum to the event principal/)
    expect(calls.some((c) => c.path === 'commands')).toBe(false)

    fireEvent.change(screen.getByLabelText('Allocation 1 principal'), { target: { value: '80.00' } })
    fireEvent.click(screen.getByText('Preview and save'))
    await waitFor(() => expect(calls.some((c) => c.path === 'commands')).toBe(true))
    const commit = calls.find((c) => c.path === 'commands')!.body as { payload: Record<string, unknown> }
    expect(commit.payload.entry_allocations).toEqual([
      { entry_anchor: 'anchor-120', principal_amount: '80.00', interest_amount: '0.00',
        fee_amount: '0.00', separate_fee_amount: '0.00' },
    ])
  })

  it('chooses an expense paid for me from the shared category list', async () => {
    await mount(payableDetail)
    fireEvent.change(screen.getByLabelText('Event kind'), { target: { value: 'EXPENSE_PAID_FOR_ME' } })
    fireEvent.change(screen.getByLabelText('Event principal'), { target: { value: '25.00' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Jo covered dinner' } })

    // Free text that is not a category is not saved as one.
    const picker = screen.getByRole('combobox', { name: 'Expense category' })
    fireEvent.change(picker, { target: { value: 'Dining' } })
    fireEvent.click(screen.getByText('Preview and save'))
    await screen.findByText(/an expense category from the list/)
    expect(calls.some((c) => c.path === 'preview')).toBe(false)

    fireEvent.change(picker, { target: { value: '' } })
    await waitFor(() => expect(screen.queryByRole('option', { name: /Retired Club/ })).not.toBeInTheDocument())
    const offered = screen.getAllByRole('option').map((o) => o.textContent)
    expect(offered).toEqual(expect.arrayContaining(['Food & Dining / Dining Out', 'Housing / Rent']))
    expect(offered.some((o) => /Internal|Unclassified/.test(o ?? ''))).toBe(false)

    fireEvent.change(picker, { target: { value: 'dining' } })
    fireEvent.click(screen.getByRole('button', { name: 'Food & Dining / Dining Out' }))
    fireEvent.click(screen.getByText('Preview and save'))
    await waitFor(() => expect(calls.some((c) => c.path === 'commands')).toBe(true))
    const commit = calls.find((c) => c.path === 'commands')!.body as { payload: Record<string, unknown> }
    expect(commit.payload).toMatchObject({ event_kind: 'EXPENSE_PAID_FOR_ME', expense_category: 'Food & Dining / Dining Out', entry_allocations: [] })
  })
})
