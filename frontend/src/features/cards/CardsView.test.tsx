import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const money = (v: string) => ({ value: v, currency: 'SGD' })
const fact = (role: string, value: string) => ({
  role, kind: 'money', value, currency: 'SGD',
  source_locator: `mari-card:${role}`, parser_version: 'p1', role_version: 'r1',
})

const page = {
  generation: 'sha256:abc',
  as_of: '2026-02-01',
  next_cursor: null,
  cards: [
    {
      account: {
        account_id: 'mari-credit-card', institution: 'MariBank', source_type: 'MARI_CARD',
        display_name: 'Mari Credit Card', masked_identifier: 'Mari Credit Card ****1234',
      },
      latest_statement: {
        statement_id: 's1', statement_date: '2026-01-21', period_end: '2026-01-20',
        closing_liability: fact('closing_liability', '250.00'),
        payment_due: fact('payment_due', '250.00'),
        minimum_payment: fact('minimum_payment', '25.00'),
        payment_due_date: { ...fact('payment_due_date', '2026-02-10'), kind: 'date' },
        credit_limit: fact('credit_limit', '5000.00'),
        reconciliation_status: 'reconciled',
      },
      statement_count: 1,
      coverage_gaps: [{ from: '2025-09-01', to: '2025-10-20' }],
      statements_missing_period: [],
      installment_obligations: [],
    },
  ],
}

const detail = {
  generation: 'sha256:abc', as_of: '2026-02-01',
  account: page.cards[0].account,
  facility: null,
  limit_basis: fact('credit_limit', '5000.00'),
  limit_observation: null,
  utilization: { available: true, basis: 'statement utilization', currency: 'SGD', credit_balance: false, ratio: '0.0500', over_limit: false, headroom: '4750.00' },
  latest_statement: page.cards[0].latest_statement,
  statement_activity: [
    {
      statement_id: 's1', statement_identity: 'mari-card:mari-credit-card:2026-01-21',
      statement_date: '2026-01-21', period_start: '2025-12-21', period_end: '2026-01-20',
      evidence_sha256: 'deadbeef',
      pockets: [
        {
          currency: 'SGD',
          opening_liability: fact('opening_liability', '300.00'),
          closing_liability: fact('closing_liability', '250.00'),
          summary_buckets: { D: fact('summary_bucket_D', '25.00') },
          due_fields: {},
          credit_limit: fact('credit_limit', '5000.00'),
          reconciliation: {
            status: 'reconciled', printed_equation: 'A-B-C+D+E-F+G+H-I',
            blocking_total: 3, blocking_passed: 3, checks: [],
          },
          fields_present: [],
        },
      ],
    },
  ],
  coverage_gaps: [{ from: '2025-09-01', to: '2025-10-20' }],
  statements_missing_period: [],
  manually_allocated_payments: [],
}

let currentDetail: Record<string, unknown> = detail
const apiMock = vi.fn(async (path: string, _body?: unknown) => {
  if (path.startsWith('reporting/workspaces/cards?')) return page
  if (path.startsWith('reporting/workspaces/cards/')) return currentDetail
  if (path === 'preview' || path === 'commands') return { status: 'applied' }
  throw new Error(`unmocked ${path}`)
})
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

async function mount(onSaved = vi.fn()) {
  const { CardsView } = await import('./index')
  return render(<CardsView generation="sha256:abc" onSaved={onSaved} />)
}

function command() {
  const call = apiMock.mock.calls.find(([path]) => path === 'commands')
  return call?.[1] as { kind: string; anchors: Record<string, string>; payload: Record<string, unknown> }
}

afterEach(() => { cleanup(); apiMock.mockClear(); currentDetail = detail })

describe('CardsView', () => {
  it('lists a card with its latest observed statement, not a timeless balance', async () => {
    await mount()
    expect(await screen.findByText('Mari Credit Card')).toBeInTheDocument()
    expect(screen.getByText('250.00 SGD')).toBeInTheDocument()
    expect(screen.getByText(/missing period/)).toBeInTheDocument()
  })

  it('sets up a facility for a card through preview, then save', async () => {
    const onSaved = vi.fn()
    await mount(onSaved)
    fireEvent.click(await screen.findByText('Mari Credit Card'))
    expect(await screen.findByText(/No credit facility is set up/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Set up facility' }))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'One shared line' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview: save facility' }))
    expect(await screen.findByText(/Creates “Mari Credit Card” in SGD, shared by Mari Credit Card/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save facility' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
    expect(command().kind).toBe('CARD_FACILITY_SET')
    expect(command().payload).toEqual({ label: 'Mari Credit Card', account_ids: ['mari-credit-card'], currency: 'SGD', reason: 'One shared line' })
  })

  it('records a dated limit on an existing facility and refuses a bad amount', async () => {
    currentDetail = { ...detail, facility: { facility_id: '33333333-3333-4333-8333-333333333333', label: 'Mari line', currency: 'SGD', bound: true, account_ids: ['mari-credit-card'] } }
    await mount()
    fireEvent.click(await screen.findByText('Mari Credit Card'))
    fireEvent.click(await screen.findByRole('button', { name: 'Record limit' }))
    fireEvent.change(screen.getByLabelText('Seen on'), { target: { value: '2026-02-01' } })
    fireEvent.change(screen.getByLabelText('Limit (SGD)'), { target: { value: '-5' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Bank app' } })
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a positive amount')
    expect(screen.getByRole('button', { name: 'Preview: save limit' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Limit (SGD)'), { target: { value: '5000' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview: save limit' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Save limit' }))
    await waitFor(() => expect(command()?.kind).toBe('CARD_LIMIT_OBSERVATION_SET'))
    expect(command().anchors.facility).toBe('33333333-3333-4333-8333-333333333333')
    expect(command().payload).toEqual({ observed_on: '2026-02-01', limit: '5000', source_ref: null, reason: 'Bank app' })
  })

  it('records that the card is paid in full each month', async () => {
    const onSaved = vi.fn()
    await mount(onSaved)
    fireEvent.click(await screen.findByText('Mari Credit Card'))
    expect(await screen.findByText(/Not set\. If your repayments can’t be matched/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'I pay it in full each month' }))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'I clear it every month' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview: save plan' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Save plan' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
    expect(command()).toMatchObject({ kind: 'CARD_REPAYMENT_PLAN_SET', anchors: { account: 'mari-credit-card' },
      payload: { rule: 'pay_in_full', reason: 'I clear it every month' } })
  })

  it('shows a declared plan and offers to remove it', async () => {
    currentDetail = { ...detail, repayment_plan: { rule: 'pay_in_full', reason: 'I clear it every month', journal_seq: 9 } }
    await mount()
    fireEvent.click(await screen.findByText('Mari Credit Card'))
    expect(await screen.findByText(/You said each statement is paid in full/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove plan' }))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Stopped' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview: remove plan' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Remove plan' }))
    await waitFor(() => expect(command()?.kind).toBe('CARD_REPAYMENT_PLAN_RETIRE'))
  })

  it('shows each statement paid or not, and how that is known', async () => {
    currentDetail = { ...detail, repayments: [
      { statement_id: 's2', statement_date: '2026-02-21', currency: 'SGD', due: '200.00', paid: '0.00', payment_due_date: '2026-03-13', paid_basis: 'none', paid_in_full: false },
      { statement_id: 's1', statement_date: '2026-01-21', currency: 'SGD', due: '250.00', paid: '250.00', payment_due_date: '2026-02-10', paid_basis: 'card_credits_in_payment_window', paid_in_full: true },
    ] }
    await mount()
    fireEvent.click(await screen.findByText('Mari Credit Card'))
    expect(await screen.findByText(/1 of 2 statements paid in full/)).toBeInTheDocument()
    expect(screen.getByText('Paid in full')).toBeInTheDocument()
    expect(screen.getByText('No payment seen yet')).toBeInTheDocument()
  })

  it('opens a card and shows the printed source equation and coverage gap', async () => {
    await mount()
    fireEvent.click(await screen.findByText('Mari Credit Card'))
    await waitFor(() => expect(screen.getByText(/All cards/)).toBeInTheDocument())
    expect(screen.getByText(/A-B-C\+D\+E-F\+G\+H-I/)).toBeInTheDocument()
    expect(screen.getByText(/Missing statement period 2025-09-01/)).toBeInTheDocument()
    expect(screen.getByText(/statement utilization/)).toBeInTheDocument()
  })
})
