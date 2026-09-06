import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const money = (v: string) => ({ value: v, currency: 'SGD' })

const page = {
  generation: 'sha256:abc',
  as_of: '2026-02-01',
  next_cursor: null,
  i_owe: [
    {
      debt_id: 'd-1', direction: 'PAYABLE', currency: 'SGD', counterparty_label: 'Alex',
      opened_on: '2026-01-05', due_on: null, status: 'OPEN', closed_on: null,
      remaining_principal: money('400.00'),
      last_event: { kind: 'BORROWED', effective_on: '2026-01-05' },
      reconciliation_status: 'opening_balance_only', event_count: 1,
    },
  ],
  owed_to_me: [
    {
      debt_id: 'd-2', direction: 'RECEIVABLE', currency: 'SGD', counterparty_label: 'Jo',
      opened_on: '2026-01-06', due_on: '2026-02-06', status: 'OPEN', closed_on: null,
      remaining_principal: money('0'),
      last_event: null, reconciliation_status: 'no_events', event_count: 0,
    },
  ],
}

const detail = {
  generation: 'sha256:abc', as_of: '2026-02-01',
  debt: {
    ...page.i_owe[0], reason: 'Owe Alex',
    principal_history: [
      {
        event_id: 'e-1', event_kind: 'BORROWED', effective_on: '2026-01-05',
        opening_balance: true, signed: 1, principal: money('400.00'),
        expense_category: null, bound: true, dangling_reason: null,
        reason: 'Opening balance owed to Alex', journal_seq: 3,
      },
    ],
  },
}

const apiMock = vi.fn(async (path: string) => {
  if (path.startsWith('debts?')) return page
  if (path.startsWith('debts/')) return detail
  throw new Error(`unmocked ${path}`)
})
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string) => apiMock(path) }
})

async function mount() {
  const { DebtsView } = await import('./index')
  return render(<DebtsView generation="sha256:abc" />)
}

afterEach(() => { cleanup(); apiMock.mockClear() })

describe('DebtsView', () => {
  it('separates payables from receivables and never nets them', async () => {
    await mount()
    expect(await screen.findByText('I owe')).toBeInTheDocument()
    expect(screen.getByText('Owed to me')).toBeInTheDocument()
    expect(screen.getByText('SGD 400.00')).toBeInTheDocument()
    expect(screen.getByText('SGD 0.00')).toBeInTheDocument()
  })

  it('opens a debt and shows its principal history', async () => {
    await mount()
    fireEvent.click(await screen.findByText('Alex'))
    await waitFor(() => expect(screen.getByText(/All debts/)).toBeInTheDocument())
    expect(screen.getByText('Remaining principal')).toBeInTheDocument()
    expect(screen.getByText(/opening/)).toBeInTheDocument()
    expect(screen.getByText('Opening balance owed to Alex')).toBeInTheDocument()
  })
})
