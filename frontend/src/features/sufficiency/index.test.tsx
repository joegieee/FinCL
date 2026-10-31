import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const loadSufficiency = vi.fn()
const setReservePolicy = vi.fn()
vi.mock('./api', () => ({
  loadSufficiency: (...a: unknown[]) => loadSufficiency(...a),
  setReservePolicy: (...a: unknown[]) => setReservePolicy(...a),
}))
vi.mock('@/lib/api', async () => {
  class ApiError extends Error { status: number; constructor(status: number, message: string) { super(message); this.status = status } }
  return { money: (v: string, c = 'SGD') => `${c} ${v}`, ApiError }
})

const { SufficiencyView, when } = await import('./index')

function kase(over: Record<string, unknown> = {}) {
  return {
    verdict: 'enough', lowest: '1500.00', lowest_on: '2026-05-27', closing: '2700.00',
    first_short_on: null, shortfall: null, shortfall_on: null, ...over,
  }
}

function combined(over: Record<string, unknown> = {}) {
  return {
    reporting_currency: 'SGD', answer: 'Yes', verdict: 'enough', complete: true, incomplete_because: [],
    starts_on: '2025-12-10', ends_on: '2026-06-10', starting_total: '6650.00',
    base: kase(), cautious: kase(), only_safe_in_the_base_case: false,
    reserve: { available: false, target: null },
    pockets: [
      { currency: 'SGD', counted: 'projected', why: null, starting_balance: '400.00', starting_balance_reported: '400.00', rate: null, rate_display: null,
        accounts: [{ account: 'a1', display_name: 'Everyday', evidence_through: '2025-12-10', age_days: 36, stale: true, basis: 'statement', holds_back_start: true }] },
      { currency: 'USD', counted: 'projected', why: null, starting_balance: '5000.00', starting_balance_reported: '6250.00', rate: '5/4', rate_display: '1.25', accounts: [] },
    ],
    income: [{ label: 'ACME CLIENT LTD', currency: 'USD', count: 6, next_on: '2026-01-25', confidence: 'likely', native_total: '9000.00', total: '11250.00', in_cautious_case: false }],
    where_it_goes: [
      { origin: 'baseline_spending', label: 'Baseline spending', total: '18000.00', monthly: '3000.00', items: [] },
      { origin: 'personal_debt', label: 'Debt repayments', total: '2000.00', monthly: '333.33', items: [{ label: 'Alex', on: '2026-03-15', amount: '2000.00', conditional: false, currency: 'SGD' }] },
    ],
    todos: [],
    upside: [],
    monthly_out: '3333.33',
    ...over,
  }
}

function page(over: Record<string, unknown> = {}) {
  return {
    version: 'sufficiency-v1', generated_for: '2026-01-15',
    reserve_policy: {
      months: '3', is_default: true, decided_at: null,
      reason: 'no reserve policy has been chosen; the framework default of 3 months of essential spending applies',
      basis: 'the month count is your policy; the monthly amount it multiplies is always the evidence-derived mean essential spend',
    },
    combined_answer: combined(),
    by_currency: {},
    limitations: [], semantics: {},
    ...over,
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks() })
beforeEach(() => { loadSufficiency.mockResolvedValue(page()) })

describe('when', () => {
  it('states the date and how far it is from today', () => {
    expect(when('2026-01-18', '2026-01-15')).toBe('18 Jan 2026 (in 3 days)')
    expect(when('2025-12-10', '2026-01-15')).toMatch(/^10 Dec 2025 \(5 weeks ago\)$/)
    expect(when('2026-06-10', '2026-01-15')).toMatch(/\(in 5 months\)$/)
  })
})

describe('SufficiencyView', () => {
  it('gives one answer for all pockets together, with no per-currency verdicts', async () => {
    render(<SufficiencyView generation="gen-1" />)

    expect(await screen.findByRole('heading', { name: 'Yes' })).toBeInTheDocument()
    expect(screen.getByText(/all accounts and currencies together/)).toBeInTheDocument()
    expect(screen.getByText('SGD 2700.00')).toBeInTheDocument()
    expect(screen.queryByText(/WILL I HAVE ENOUGH\? ·/)).toBeNull()
    expect(screen.queryByText(/transfer/i)).toBeNull()
  })

  it('says how short, from when, when the pool runs out', async () => {
    loadSufficiency.mockResolvedValue(page({ combined_answer: combined({
      answer: 'No', verdict: 'short',
      base: kase({ verdict: 'short', first_short_on: '2026-02-18', shortfall: '1240.00', shortfall_on: '2026-02-28' }),
    }) }))

    render(<SufficiencyView generation="gen-1" />)

    expect(await screen.findByRole('heading', { name: 'No' })).toBeInTheDocument()
    expect(screen.getByText(/You run out around 18 Feb 2026/)).toHaveTextContent('SGD 1240.00 short')
  })

  it('keeps a reserve dip separate from running out of cash', async () => {
    loadSufficiency.mockResolvedValue(page({ combined_answer: combined({
      answer: 'Yes, but it eats into your reserve', verdict: 'below_reserve',
      base: kase({ verdict: 'below_reserve', first_short_on: '2026-03-20', shortfall: '740.00', shortfall_on: '2026-03-20' }),
    }) }))

    render(<SufficiencyView generation="gen-1" />)

    expect(await screen.findByText(/eats into your reserve/)).toBeInTheDocument()
    expect(screen.getByText(/You don’t run out, but you dip into your safety reserve/)).toBeInTheDocument()
  })

  it('never lets a yes hide debts that have no due date', async () => {
    loadSufficiency.mockResolvedValue(page({ combined_answer: combined({
      not_in_the_path: { as_of: '2026-01-15', total: '15000.00', unconverted: [], short_if_repaid_now: '8350.00',
        items: [{ label: 'Creditor A', origin: 'personal_debt', amount: '5000.00', native: { value: '5000.00', currency: 'SGD' } }] },
    }) }))

    render(<SufficiencyView generation="gen-1" />)

    expect(await screen.findByRole('heading', { name: 'Yes, but only if your debts can wait' })).toBeInTheDocument()
    expect(screen.getByText(/you owe has no due date/).closest('div')).toHaveTextContent('Repaying it all now would leave you SGD 8350.00 short')
  })

  it('names a pocket it could not count beside the answer', async () => {
    loadSufficiency.mockResolvedValue(page({ combined_answer: combined({
      complete: false,
      pockets: [
        { currency: 'SGD', counted: 'projected', why: null, starting_balance: '400.00', starting_balance_reported: '400.00', rate: null, rate_display: null, accounts: [] },
        { currency: 'THB', counted: 'not_counted', why: 'no exchange rate to the reporting currency is available', starting_balance: '0.50', starting_balance_reported: null, rate: null, rate_display: null, accounts: [] },
      ],
    }) }))

    render(<SufficiencyView generation="gen-1" />)

    expect(await screen.findByRole('note')).toHaveTextContent('Not counted: THB 0.50 (no exchange rate to the reporting currency is available)')
    expect(screen.getByText('not counted')).toBeInTheDocument()
  })

  it('says beside a yes why it is incomplete', async () => {
    loadSufficiency.mockResolvedValue(page({ combined_answer: combined({
      answer: 'Yes, but not every bill is counted', complete: false,
      incomplete_because: ['Rewards Card (SGD): its bills can’t be dated or sized from the statements, so none are counted.'],
    }) }))

    render(<SufficiencyView generation="gen-1" />)

    expect(await screen.findByRole('heading', { name: 'Yes, but not every bill is counted' })).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent('This answer is incomplete because:Rewards Card (SGD)')
  })

  it('says how fresh each account is and which one holds the start back', async () => {
    render(<SufficiencyView generation="gen-1" />)

    expect(await screen.findByText(/^Everyday: proven through 10 Dec 2025/)).toHaveTextContent('out of date · holding the start back')
  })

  it('offers a way in to add something you know is coming', async () => {
    const onOpenPlanned = vi.fn()
    render(<SufficiencyView generation="gen-1" onOpenPlanned={onOpenPlanned} />)
    fireEvent.click(await screen.findByRole('button', { name: /Add something you know is coming/ }))

    expect(onOpenPlanned).toHaveBeenCalled()
  })

  it('notes a pocket that is projected without spending history', async () => {
    loadSufficiency.mockResolvedValue(page({ combined_answer: combined({
      pockets: [{ currency: 'USD', counted: 'projected', why: 'it has no spending history, so only what is scheduled for it is counted', starting_balance: '500.00', starting_balance_reported: '500.00', rate: null, rate_display: null, accounts: [] }],
    }) }))
    render(<SufficiencyView generation="gen-1" />)

    expect(await screen.findByText(/it has no spending history, so only what is scheduled/)).toBeInTheDocument()
  })

  it('lands an import to-do on the coverage card', async () => {
    const onOpen = vi.fn()
    loadSufficiency.mockResolvedValue(page({ combined_answer: combined({ todos: [
      { key: 'recent-statements', page: 'Setup', anchor: 'setup-coverage', title: 'Import your latest statements', detail: 'Everyday' },
    ] }) }))

    render(<SufficiencyView generation="gen-1" onOpen={onOpen} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Import your latest statements: open Setup' }))

    expect(window.location.hash).toBe('#setup-coverage')
    expect(onOpen).toHaveBeenCalledWith('Setup')
  })

  it('lists money that may come back without counting it', async () => {
    loadSufficiency.mockResolvedValue(page({ combined_answer: combined({
      upside: [{ label: 'Jo', currency: 'SGD', amount: '800.00', on: '2026-02-01' }],
    }) }))

    render(<SufficiencyView generation="gen-1" />)

    expect(await screen.findByText('Not counted, in case it doesn’t come:')).toBeInTheDocument()
    expect(screen.getByText(/^Jo ·/).closest('li')).toHaveTextContent('SGD 800.00')
  })

  it('lists a pocket too small to matter as ignored, without flagging the answer', async () => {
    loadSufficiency.mockResolvedValue(page({ combined_answer: combined({
      pockets: [
        { currency: 'SGD', counted: 'projected', why: null, starting_balance: '400.00', starting_balance_reported: '400.00', rate: null, rate_display: null, accounts: [] },
        { currency: 'THB', counted: 'ignored', why: 'it holds less than one unit of its currency and nothing is scheduled', starting_balance: '0.50', starting_balance_reported: null, rate: null, rate_display: null, accounts: [] },
      ],
    }) }))

    render(<SufficiencyView generation="gen-1" />)

    expect(await screen.findByText('ignored, too small to matter')).toBeInTheDocument()
    expect(screen.queryByRole('note')).toBeNull()
  })

  it('never renders a missing answer as a yes', async () => {
    loadSufficiency.mockResolvedValue(page({
      combined_answer: combined({ answer: 'Not enough evidence to say', verdict: 'unavailable', pockets: [
        { currency: 'SGD', counted: 'not_counted', why: 'no supported opening balance', starting_balance: null, starting_balance_reported: null, rate: null, rate_display: null, accounts: [] },
      ] }),
      limitations: ['included liquid pockets do not share a coverage-backed balance cutoff'],
    }))
    const onOpenCash = vi.fn()

    render(<SufficiencyView generation="gen-1" onOpenCash={onOpenCash} />)

    expect(await screen.findByText('There isn’t enough to go on yet')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Yes' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'See which account is holding this back' }))
    expect(onOpenCash).toHaveBeenCalled()
  })

  it('states the cautious case beside the answer and flags when it is the only thing holding', async () => {
    loadSufficiency.mockResolvedValue(page({ combined_answer: combined({
      only_safe_in_the_base_case: true,
      cautious: kase({ verdict: 'short', first_short_on: '2026-01-20', shortfall: '30000.00' }),
    }) }))

    render(<SufficiencyView generation="gen-1" />)

    expect(await screen.findByText(/Cautious case/)).toBeInTheDocument()
    expect(screen.getByText(/up to SGD 30000.00 short/)).toBeInTheDocument()
    expect(screen.getByText(/depends on that income arriving/)).toBeInTheDocument()
  })

  it('lists what would make the answer better, each opening the page that fixes it', async () => {
    loadSufficiency.mockResolvedValue(page({ combined_answer: combined({ todos: [
      { key: 'recent-statements', page: 'Setup', title: 'Import your latest statements', detail: 'Nothing has been imported after 2025-12-10.' },
      { key: 'debt-dates', page: 'Debts', title: 'Give your debts a due date', detail: 'Creditor A, Creditor B, Creditor C.' },
    ] }) }))
    const onOpen = vi.fn()

    render(<SufficiencyView generation="gen-1" onOpen={onOpen} />)

    expect(await screen.findByText('2 things to fix, most useful first')).toBeInTheDocument()
    expect(screen.getByText(/Nothing has been imported after 10 Dec 2025\./)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Give your debts a due date: open Debts' }))
    expect(onOpen).toHaveBeenCalledWith('Debts')
  })

  it('says in plain words what the answer assumes, in one list of where the money goes', async () => {
    const onOpenObligations = vi.fn()
    render(<SufficiencyView generation="gen-1" onOpenObligations={onOpenObligations} />)

    expect(await screen.findByText('WHAT THIS ASSUMES')).toBeInTheDocument()
    expect(screen.getByText(/at 1 USD = 1.25 SGD/)).toBeInTheDocument()
    expect(screen.getByText(/likely, so left out of the cautious case/)).toBeInTheDocument()
    expect(screen.getByText(/Alex · 15 Mar 2026/)).toBeInTheDocument()
    expect(screen.getByText(/Money going out · about SGD 3333.33 a month/)).toBeInTheDocument()
    expect(screen.queryByText('WHAT DRIVES IT')).toBeNull()
    expect(screen.queryByText('EVERYTHING THIS ANSWER COUNTS')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'See everything owed' }))
    expect(onOpenObligations).toHaveBeenCalled()
  })

  it('distinguishes the framework default from a chosen policy', async () => {
    render(<SufficiencyView generation="gen-1" />)

    expect(await screen.findByRole('button', { name: 'Choose a policy' })).toBeInTheDocument()
  })

  it('records a chosen reserve policy as a month count with a reason', async () => {
    setReservePolicy.mockResolvedValue({ status: 'committed' })
    render(<SufficiencyView generation="gen-1" />)

    fireEvent.click(await screen.findByRole('button', { name: 'Choose a policy' }))
    fireEvent.change(screen.getByLabelText('Months of essential spending'), { target: { value: '6' } })
    fireEvent.change(screen.getByLabelText('Why this many?'), { target: { value: 'contract work' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save reserve policy' }))

    await waitFor(() => expect(setReservePolicy).toHaveBeenCalledWith('gen-1', '6', 'contract work'))
  })

  it('refuses a month count outside the supported range', async () => {
    render(<SufficiencyView generation="gen-1" />)

    fireEvent.click(await screen.findByRole('button', { name: 'Choose a policy' }))
    fireEvent.change(screen.getByLabelText('Months of essential spending'), { target: { value: '40' } })
    fireEvent.change(screen.getByLabelText('Why this many?'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save reserve policy' }))

    expect(await screen.findByText('Choose between 0.5 and 12 months.')).toBeInTheDocument()
    expect(setReservePolicy).not.toHaveBeenCalled()
  })

  it('offers no amount field on the reserve policy form', async () => {
    render(<SufficiencyView generation="gen-1" />)

    fireEvent.click(await screen.findByRole('button', { name: 'Choose a policy' }))

    expect(screen.queryByLabelText(/amount/i)).toBeNull()
    expect(screen.getByText(/never entered here/)).toBeInTheDocument()
  })
})
