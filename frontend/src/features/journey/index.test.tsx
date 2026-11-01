import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { lastFullMonth } from './api'

const journey = {
  generated_for: '2026-03-05', reporting_currency: 'SGD',
  statements: {
    accounts: [
      { account_id: 'card', name: 'Mari Credit Card', statements: 12, through: '2026-01-10', days_behind: 54, behind: true },
      { account_id: 'dbs', name: 'DBS My Account', statements: 12, through: '2026-01-21', days_behind: 43, behind: false },
    ],
    complete_through: '2026-01-10', latest: '2026-01-21', behind_count: 1, quarantined_count: 0, stale_after_days: 45,
  },
  owed: {
    as_of: '2026-03-05', total: '15200.00', unknown_amount_count: 0, unconverted_count: 0,
    owed_to_you: { count: 0, total: '0.00', unknown: 0 },
    groups: [
      { origin: 'personal_debt', label: 'Debts you owe', count: 3, total: '15000.00', by_currency: { SGD: '15000.00' }, unknown_amount: [], unconverted: [], next_due: null },
      { origin: 'card_statement', label: 'Credit card bills', count: 1, total: '200.00', by_currency: { SGD: '200.00' }, unknown_amount: [], unconverted: [], next_due: null },
      { origin: 'recurring_commitment', label: 'Regular commitments', count: 0, total: '0.00', by_currency: {}, unknown_amount: [], unconverted: [], next_due: null },
    ],
  },
}
const total = (value: string) => ({ total: { value, currency: 'SGD' }, partial_total: null, unavailable_reasons: [], excluded_detail: [] })
const analysis = {
  reporting_currency: 'SGD', summary: { spending: { current: total('1500.00') } },
  category_ranking: [{ name: 'Food & Dining / Groceries', result: total('400.00'), handle: 'h1' }, { name: 'Housing / Rent', result: total('300.00'), handle: 'h2' }],
}
const sufficiency = (over: Record<string, unknown> = {}) => ({ combined_answer: {
  reporting_currency: 'SGD', answer: 'Yes', verdict: 'enough', ends_on: '2026-09-05',
  base: { verdict: 'enough', lowest: '1500.00', lowest_on: '2026-08-27', closing: '2700.00', shortfall: null, shortfall_on: null, first_short_on: null },
  cautious: { verdict: 'enough', shortfall: null },
  pockets: [{ currency: 'SGD', counted: 'projected' }],
  not_in_the_path: { as_of: '2026-03-05', total: '15000.00', items: [{ label: 'Creditor A' }, { label: 'Creditor B' }, { label: 'Creditor C' }], unconverted: [], short_if_repaid_now: '8350.00' },
  ...over,
} })

const pocket = (closing: string, cautious: string) => ({
  available: true, starting_balance: '1000.00', starting_balance_as_of: '2026-01-31',
  base: { closing_balance: closing, days: [{ date: '2026-02-01', closing }] },
  conservative: { closing_balance: cautious, days: [{ date: '2026-02-01', closing: cautious }] },
})

const apiMock = vi.fn()
const analysisMock = vi.fn()
const sufficiencyMock = vi.fn()
vi.mock('@/lib/api', async original => ({ ...await original<typeof import('@/lib/api')>(), api: (...a: unknown[]) => apiMock(...a) }))
vi.mock('@/features/analysis/reportedApi', async original => ({ ...await original<typeof import('@/features/analysis/reportedApi')>(), loadReportedAnalysis: (...a: unknown[]) => analysisMock(...a) }))
vi.mock('@/features/sufficiency/api', async original => ({ ...await original<typeof import('@/features/sufficiency/api')>(), loadSufficiency: () => sufficiencyMock() }))
afterEach(() => { cleanup(); vi.clearAllMocks() })

async function mount(onOpen = vi.fn(), page = sufficiency()) {
  apiMock.mockImplementation(async (path: string) => { if (path === 'journey') return journey; throw new Error(path) })
  analysisMock.mockResolvedValue(analysis)
  sufficiencyMock.mockResolvedValue(page)
  const { MoneyJourneyPanel } = await import('./index')
  render(<MoneyJourneyPanel generation="g1" onOpen={onOpen} />)
  return onOpen
}

describe('lastFullMonth', () => {
  it('is the month itself when the statements reach its last day, else the month before', () => {
    expect(lastFullMonth('2026-08-31')).toEqual(['2026-08-01', '2026-08-31'])
    expect(lastFullMonth('2026-08-20')).toEqual(['2026-07-01', '2026-07-31'])
    expect(lastFullMonth('2026-01-15')).toEqual(['2025-12-01', '2025-12-31'])
  })
})

describe('CashTimeline', () => {
  it('shows one pocket at a time, opening on the reporting currency, and names the ones it cannot forecast', async () => {
    const { CashTimeline } = await import('./CashTimeline')
    const page = { available: true, by_currency: { EUR: pocket('50.00', '40.00'), SGD: pocket('900.00', '-200.00'), USD: { available: false, reason: 'x' } } }
    render(<CashTimeline page={page as never} today="2026-03-05" reportingCurrency="SGD" />)
    expect(screen.getByText('SGD 900.00')).toBeInTheDocument()
    expect(screen.getByText(/No forecast for USD/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'EUR' }))
    expect(screen.getByText('EUR 50.00')).toBeInTheDocument()
    expect(screen.queryByText('SGD 900.00')).toBeNull()
  })
})

describe('MoneyJourneyPanel', () => {
  it('shows how far statements reach and which account holds them back', async () => {
    const onOpen = await mount()
    expect(await screen.findByText(/10 Jan 2026 \(8 weeks ago\)/)).toBeInTheDocument()
    expect(screen.getByText('Mari Credit Card').closest('li')).toHaveClass('is-behind')
    fireEvent.click(screen.getByRole('button', { name: 'Statements' }))
    expect(onOpen).toHaveBeenCalledWith('Setup')
  })

  it('adds up the last full month of spending, by category', async () => {
    await mount()
    expect(await screen.findByText(/December 2025/)).toBeInTheDocument()
    expect(analysisMock).toHaveBeenCalledWith(expect.objectContaining({ historyStart: '2025-12-01', historyEnd: '2025-12-31' }))
    expect(screen.getByText('Groceries')).toBeInTheDocument()
  })

  it('totals what is owed by kind, leaving out kinds with nothing', async () => {
    await mount()
    expect(await screen.findByText('SGD 15,200.00')).toBeInTheDocument()
    expect(screen.getByText('Debts you owe')).toBeInTheDocument()
    expect(screen.queryByText('Regular commitments')).toBeNull()
  })

  it('never lets a yes hide debts that have no due date', async () => {
    await mount()
    expect(await screen.findByText('Only if your debts can wait')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent('SGD 15,000.00 you owe has no due date')
    expect(screen.getByRole('note')).toHaveTextContent('SGD 8,350.00 short')
  })

  it('says yes plainly when nothing is left out', async () => {
    await mount(vi.fn(), sufficiency({ not_in_the_path: { as_of: null, total: '0.00', items: [], unconverted: [], short_if_repaid_now: null } }))
    expect(await screen.findByText('Yes')).toBeInTheDocument()
    expect(screen.queryByRole('note')).toBeNull()
  })

  it('states the shortfall when the money runs out', async () => {
    await mount(vi.fn(), sufficiency({ verdict: 'short', base: { verdict: 'short', shortfall: '1240.00', shortfall_on: '2026-12-28', lowest: '-1240.00', closing: '-900.00' } }))
    expect(await screen.findByText(/No — short by SGD 1,240.00/)).toBeInTheDocument()
  })
})
