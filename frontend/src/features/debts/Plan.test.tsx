import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const money = (v: string) => ({ value: v, currency: 'SGD' })

const row = (index: number, due: string, over: Record<string, unknown> = {}) => ({
  schedule_index: index, due_on: due, amount: '300.00', paid: '0.00', remaining: '300.00', trimmed: '0.00',
  was_trimmed: false, state: 'upcoming', overdue: false, ...over,
})
const plan = {
  effective_from: '2026-01-05', plan_kind: 'rows', reason: 'Agreed with Alex',
  rows: [row(0, '2026-01-20', { paid: '300.00', remaining: '0.00', state: 'paid' }),
    row(1, '2026-01-25', { paid: '100.00', remaining: '200.00', state: 'overdue', overdue: true }),
    row(2, '2026-03-20')],
  remaining_principal: '700.00', scheduled_remaining: '500.00',
  unscheduled_remainder: { amount: '200.00', due_on: null, overdue: false },
  next_due: '2026-01-25', overdue_count: 1,
}
const card = {
  debt_id: 'd-1', direction: 'PAYABLE', currency: 'SGD', counterparty_label: 'Alex',
  opened_on: '2026-01-05', due_on: null, status: 'OPEN', closed_on: null,
  remaining_principal: money('700.00'), last_event: null, reconciliation_status: 'reconciled', event_count: 1,
}
const page = { generation: 'g1', as_of: '2026-02-01', next_cursor: null, i_owe: [{ ...card, plan: { next_due: '2026-01-25', overdue_count: 1, unscheduled_remainder: true } }], owed_to_me: [] }
const detail = (extra: Record<string, unknown> = {}) => ({
  generation: 'g1', as_of: '2026-02-01',
  debt: { ...card, reason: 'Owe Alex', principal_history: [], schedule: null, overlapping_commitments: [], ...extra },
})

const apiMock = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})
const calls = (path: string) => apiMock.mock.calls.filter(([p]) => p === path)

function serve(detailBody: unknown, extra: (path: string, body?: unknown) => unknown = () => undefined) {
  apiMock.mockImplementation(async (path: string, body?: unknown) => {
    const other = extra(path, body)
    if (other !== undefined) return other
    if (path.startsWith('reporting/workspaces/debts?')) return page
    if (path.startsWith('reporting/workspaces/debts/')) return detailBody
    if (path === 'batch') return { status: 'applied' }
    throw new Error(`unmocked ${path}`)
  })
}
async function open() {
  const { DebtsView } = await import('./index')
  const onSaved = vi.fn()
  render(<DebtsView generation="g1" onSaved={onSaved} />)
  fireEvent.click(await screen.findByText('Alex'))
  return onSaved
}
afterEach(() => { cleanup(); apiMock.mockReset() })

describe('Repayment plan', () => {
  it('says what is next on the list and lays the rows against what was paid', async () => {
    serve(detail({ schedule: plan }))
    const { DebtsView } = await import('./index')
    render(<DebtsView generation="g1" />)
    expect(await screen.findByText(/next payment .* · overdue/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('Alex'))
    expect(await screen.findByText('Repayment plan')).toBeInTheDocument()
    const states = screen.getAllByRole('row').map(r => r.textContent)
    expect(states.some(t => t?.includes('SGD 300.00') && t.endsWith('Paid'))).toBe(true)
    expect(states.some(t => t?.includes('2026') && t.includes('SGD 200.00') && t.endsWith('Overdue'))).toBe(true)
    expect(states.some(t => t?.endsWith('Upcoming'))).toBe(true)
    expect(screen.getByText(/past due and not covered/)).toBeInTheDocument()
    expect(screen.getByText(/has no due date, so the forecast cannot place it/)).toBeInTheDocument()
  })

  it('previews a repeating plan until paid and saves exactly what the server returned', async () => {
    const saved = [{ kind: 'PERSONAL_DEBT_SCHEDULE_SET', anchors: { debt: 'd-1' }, payload: { effective_from: '2026-02-01', reason: 'Pay Alex monthly', plan: { kind: 'rows', rows: [{ due_on: '2026-03-01', amount: '500.00' }, { due_on: '2026-04-01', amount: '200.00' }] } } }]
    const after = { ...plan, overdue_count: 0, unscheduled_remainder: null, scheduled_remaining: '700.00', rows: [row(0, '2026-03-01', { amount: '500.00', remaining: '500.00' }), row(1, '2026-04-01', { amount: '200.00', remaining: '200.00' })] }
    serve(detail(), (path) => path === 'debts/preview' ? { generation: 'g1', items: saved, debts: [{ debt_id: 'd-1', schedule: { before: null, after } }] } : undefined)
    const onSaved = await open()

    fireEvent.click(await screen.findByRole('button', { name: 'Add a plan' }))
    fireEvent.change(screen.getByLabelText('Payment amount'), { target: { value: '500' } })
    fireEvent.change(screen.getByLabelText('First payment on'), { target: { value: '2026-03-01' } })
    fireEvent.change(screen.getByLabelText('Plan reason'), { target: { value: 'Pay Alex monthly' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check plan' }))

    expect(await screen.findByText(/covers SGD 700.00 of the SGD 700.00 still owed/)).toBeInTheDocument()
    const [, draft] = calls('debts/preview')[0] as [string, { items: { payload: Record<string, unknown> }[] }]
    expect(draft.items[0].payload).toEqual({
      effective_from: '2026-02-01', reason: 'Pay Alex monthly',
      plan: { kind: 'series', schedule: { kind: 'monthly', day: 1, anchor_on: '2026-03-01' }, amount: '500', count: null },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save plan' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
    expect(calls('batch')[0][1]).toMatchObject({ items: saved, reason: 'Pay Alex monthly' })
  })

  it('offers the overlapping commitment as the plan and saves the batch the server built', async () => {
    const items = [
      { kind: 'PERSONAL_DEBT_SCHEDULE_SET', anchors: { debt: 'd-1' }, payload: { effective_from: '2026-02-04', plan: { kind: 'rows', rows: [{ due_on: '2026-03-03', amount: '19.00' }] }, reason: 'x' } },
      { kind: 'RECURRING_COMMITMENT_SET', anchors: { commitment: 'c-1' }, payload: { status: 'ended', ended_on: '2026-02-03', reason: 'x' } },
    ]
    serve(detail({ overlapping_commitments: [{ commitment: 'c-1', name: 'Alex repayment', payments: 2 }] }), (path, body) => {
      if (path.startsWith('debts/d-1/commitment-schedule?')) return { generation: 'g1', items }
      if (path === 'debts/preview') return { generation: 'g1', items: (body as { items: unknown[] }).items, debts: [{ debt_id: 'd-1', schedule: { before: null, after: { ...plan, unscheduled_remainder: null } } }] }
      return undefined
    })
    const onSaved = await open()

    fireEvent.click(await screen.findByRole('button', { name: 'Use this commitment as the debt’s schedule' }))
    fireEvent.change(screen.getByLabelText('Plan reason'), { target: { value: 'Same payments' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check plan' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Use as the plan' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    const sent = (calls('batch')[0][1] as { items: { kind: string; payload: Record<string, unknown> }[] }).items
    expect(sent.map(i => i.kind)).toEqual(['PERSONAL_DEBT_SCHEDULE_SET', 'RECURRING_COMMITMENT_SET'])
    expect(sent[0].payload.plan).toEqual(items[0].payload.plan)
  })
})
