import { afterEach, describe, expect, it, onTestFinished, vi } from 'vitest'
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

const apiMock = vi.fn(async (path: string, _body?: unknown) => {
  if (path.startsWith('reporting/workspaces/debts?')) return page
  if (path.startsWith('reporting/workspaces/debts/')) return detail
  if (path === 'preview' || path === 'commands') return { status: 'applied' }
  throw new Error(`unmocked ${path}`)
})
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

async function mount(onSaved = vi.fn()) {
  const { DebtsView } = await import('./index')
  return render(<DebtsView generation="sha256:abc" onSaved={onSaved} />)
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

  it('flags a debt with no due date and lets one be given', async () => {
    await mount()
    expect(await screen.findByText(/no due date — not in the forecast/)).toBeInTheDocument()
    fireEvent.click(screen.getByText('Alex'))
    expect(await screen.findByText('Give this debt a due date')).toBeInTheDocument()
    expect(screen.getByText(/isn’t in your forecast/)).toBeVisible()
    fireEvent.change(screen.getByLabelText('Due date'), { target: { value: '2026-12-31' } })
    fireEvent.change(screen.getByLabelText('Details reason'), { target: { value: 'Agreed with Alex' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('commands', expect.objectContaining({
      kind: 'PERSONAL_DEBT_DETAILS_SET', anchors: { debt: 'd-1' },
      payload: { counterparty_label: 'Alex', due_on: '2026-12-31', reason: 'Agreed with Alex' },
    })))
  })
})

describe('DebtsView paging', () => {
  it('lists every debt, not only the first page', async () => {
    const second = { ...page, next_cursor: null, i_owe: [{ ...page.i_owe[0], debt_id: 'd-9', counterparty_label: 'Zed' }], owed_to_me: [] }
    const original = apiMock.getMockImplementation()!
    onTestFinished(() => { apiMock.mockImplementation(original) })
    apiMock.mockImplementation(async (path: string) => {
      if (path.startsWith('reporting/workspaces/debts?') && path.includes('cursor=d-2')) return second
      if (path.startsWith('reporting/workspaces/debts?')) return { ...page, next_cursor: 'd-2', i_owe: [...page.i_owe], owed_to_me: [...page.owed_to_me] }
      throw new Error(`unmocked ${path}`)
    })
    await mount()
    expect(await screen.findByText('Zed')).toBeInTheDocument()
    expect(screen.getByText('Alex')).toBeInTheDocument()
    expect(screen.getByText('Jo')).toBeInTheDocument()
  })
})

describe('DebtsView commitment overlap', () => {
  it('warns when a recurring commitment repeats the debt’s repayments', async () => {
    const original = apiMock.getMockImplementation()!
    onTestFinished(() => { apiMock.mockImplementation(original) })
    apiMock.mockImplementation(async (path: string) => {
      if (path.startsWith('reporting/workspaces/debts?')) return page
      if (path.startsWith('reporting/workspaces/debts/')) return { ...detail, debt: { ...detail.debt, overlapping_commitments: [{ commitment: 'c-1', name: 'Alex repayment', payments: 2 }] } }
      throw new Error(`unmocked ${path}`)
    })
    await mount()
    fireEvent.click(await screen.findByText('Alex'))
    expect(await screen.findByRole('note')).toHaveTextContent('“Alex repayment”, a confirmed recurring commitment')
    expect(screen.getByRole('note')).toHaveTextContent('counted twice')
  })
})
