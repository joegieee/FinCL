import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

const apiMock = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

const loadBudgets = vi.fn()
const loadBudgetHistory = vi.fn()
const loadGoals = vi.fn()
const loadRecurring = vi.fn()
const loadOccurrences = vi.fn()
const loadBackdatePreview = vi.fn()
const loadGoalScenario = vi.fn()
vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    loadBudgets: (...a: unknown[]) => loadBudgets(...a),
    loadBudgetHistory: (...a: unknown[]) => loadBudgetHistory(...a),
    loadGoals: (...a: unknown[]) => loadGoals(...a),
    loadRecurring: () => loadRecurring(),
    loadOccurrences: (...a: unknown[]) => loadOccurrences(...a),
    loadBackdatePreview: (...a: unknown[]) => loadBackdatePreview(...a),
    loadGoalScenario: (...a: unknown[]) => loadGoalScenario(...a),
  }
})

const { PlanningView } = await import('./index')
const CATEGORIES = ['Food & Dining / Groceries', 'Internal / Transfer']

function budgetsPage(over: Record<string, unknown> = {}) {
  return {
    generation: 'g1', month: '2026-03', currency: 'SGD', month_covered: true, month_complete: false,
    budgets: [{
      plan: 'b1', name: 'Dining', currency: 'SGD', category_key: 'Food & Dining / Groceries',
      effective_month: '2026-01', limit: '300.00', actual: '120.00', remaining: null,
      archived: false, status: 'observed_so_far',
    }],
    ...over,
  }
}
const goalsPage = {
  generation: 'g1', as_of: '2026-03-01',
  goals: [{
    plan: 'gg1', name: 'Trip', currency: 'SGD', target: '2000.00', target_on: '2026-12-01',
    observed_on: '2026-02-01', saved: '500.00', remaining: '1500.00', dated_progress_known: true,
    archived: false, overdue: false,
  }],
}
const recurringPage = { generation: 'g1', confirmed: [], paused_or_ended: [], dismissed_candidate_keys: [] }

function setup() {
  loadBudgets.mockResolvedValue(budgetsPage())
  loadGoals.mockResolvedValue(goalsPage)
  loadRecurring.mockResolvedValue(recurringPage)
  loadBackdatePreview.mockResolvedValue({ plan: 'b1', affected_months: [] })
  loadGoalScenario.mockResolvedValue({
    plan: 'gg1', currency: 'SGD', remaining: '0.00', contribution_dates_considered: [],
    monthly_amount: null, status: 'reached', assumes_no_investment_return: true,
  })
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('PlanningView', () => {
  it('shows a budget with its actual and an incomplete-month note instead of a remaining', async () => {
    setup()
    render(<PlanningView generation="g1" freshness="current" categories={CATEGORIES} onSaved={() => {}} />)
    expect(await screen.findByText('Dining')).toBeInTheDocument()
    expect(screen.getByText('SGD 120.00')).toBeInTheDocument()
    expect(screen.getByText(/no confident remaining for an incomplete month/i)).toBeInTheDocument()
  })

  it('revises a budget through preview then commit with a fresh revision id', async () => {
    setup()
    const onSaved = vi.fn()
    render(<PlanningView generation="g1" freshness="current" categories={CATEGORIES} onSaved={onSaved} />)
    await screen.findByText('Dining')

    fireEvent.click(screen.getByRole('button', { name: 'Revise' }))
    const form = within(document.querySelector('.budget-row .planning-form') as HTMLElement)
    fireEvent.change(form.getByLabelText('Monthly limit'), { target: { value: '350.00' } })
    fireEvent.change(form.getByLabelText('Reason'), { target: { value: 'raise it' } })

    apiMock.mockResolvedValueOnce({})
    fireEvent.click(form.getByRole('button', { name: 'Preview' }))
    await screen.findByText(/Applies from that month until the next revision/i)

    apiMock.mockResolvedValueOnce({ status: 'recorded' })
    fireEvent.click(form.getByRole('button', { name: 'Save revision' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))

    const preview = apiMock.mock.calls[0][1] as { kind: string; anchors: { plan: string; revision: string } }
    expect(preview.kind).toBe('BUDGET_REVISION_SET')
    expect(preview.anchors.plan).toBe('b1')
    expect(preview.anchors.revision).toMatch(/[0-9a-f-]{36}/)
  })

  it('records an absolute goal observation', async () => {
    setup()
    const onSaved = vi.fn()
    render(<PlanningView generation="g1" freshness="current" categories={CATEGORIES} onSaved={onSaved} />)
    await screen.findByText('Trip')

    fireEvent.click(screen.getByRole('button', { name: 'Add progress' }))
    const form = within([...document.querySelectorAll('.budget-row .planning-form')].at(-1) as HTMLElement)
    fireEvent.change(form.getByLabelText('Observed on'), { target: { value: '2026-03-01' } })
    fireEvent.change(form.getByLabelText('Amount saved'), { target: { value: '750.00' } })
    fireEvent.change(form.getByLabelText('Reason'), { target: { value: 'monthly top-up' } })

    apiMock.mockResolvedValueOnce({})
    fireEvent.click(form.getByRole('button', { name: 'Preview' }))
    await screen.findByText(/absolute amount, not a change/i)

    apiMock.mockResolvedValueOnce({ status: 'recorded' })
    fireEvent.click(form.getByRole('button', { name: 'Record progress' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))
    const commit = apiMock.mock.calls.find(c => c[0] === 'commands')?.[1] as { kind: string; payload: { saved: string } }
    expect(commit.kind).toBe('GOAL_OBSERVATION_SET')
    expect(commit.payload.saved).toBe('750.00')
  })

  it('shows the backdating impact when a revision starts before this month', async () => {
    setup()
    render(<PlanningView generation="g1" freshness="current" categories={CATEGORIES} onSaved={() => {}} />)
    await screen.findByText('Dining')
    loadBackdatePreview.mockResolvedValue({
      plan: 'b1',
      affected_months: [
        { month: '2020-01', previous_limit: '300.00', new_limit: '500.00', actual: '10.00', previous_remaining: '290.00', new_remaining: '490.00' },
      ],
    })
    fireEvent.click(screen.getByRole('button', { name: 'Revise' }))
    const form = within(document.querySelector('.budget-row .planning-form') as HTMLElement)
    fireEvent.change(form.getByLabelText('From month'), { target: { value: '2020-01' } })
    fireEvent.change(form.getByLabelText('Monthly limit'), { target: { value: '500.00' } })

    await waitFor(() => expect(screen.getByText(/BACKDATING AFFECTS 1 MONTH/i)).toBeInTheDocument())
    expect(screen.getByText('2020-01')).toBeInTheDocument()
    expect(loadBackdatePreview).toHaveBeenCalledWith('b1', '2020-01', '500.00', 'SGD', 'Food & Dining / Groceries')
  })

  it('estimates a monthly contribution from the goal scenario', async () => {
    setup()
    render(<PlanningView generation="g1" freshness="current" categories={CATEGORIES} onSaved={() => {}} />)
    await screen.findByText('Trip')
    fireEvent.click(screen.getByRole('button', { name: 'Plan contributions' }))

    loadGoalScenario.mockResolvedValueOnce({
      plan: 'gg1', currency: 'SGD', remaining: '1500.00',
      contribution_dates_considered: ['2026-04-01', '2026-05-01', '2026-06-01'],
      monthly_amount: '500.00', status: 'scheduled', assumes_no_investment_return: true,
    })
    fireEvent.click(screen.getByRole('button', { name: 'Estimate' }))
    expect(await screen.findByText(/SGD 500.00/)).toBeInTheDocument()
    expect(screen.getByText(/no interest or investment return is assumed/i)).toBeInTheDocument()
  })

  it('shows each occurrence at its own amount and sets a different one', async () => {
    setup()
    loadRecurring.mockResolvedValue({
      ...recurringPage,
      confirmed: [{
        commitment: 'c1', name: 'Power', currency: 'SGD', merchant_key: 'powerco', status: 'active', ended_on: null,
        schedule: { kind: 'monthly', day: 10, anchor_on: '2026-01-10' }, account_ids: [],
        expected_amount: '80.00', amount_tolerance: '5.00',
      }],
    })
    loadOccurrences.mockResolvedValue({
      generation: 'g1', commitment: 'c1', occurrences: [
        { scheduled_on: '2026-11-10', state: 'upcoming_estimate', entry: null, expected_amount: '80.00', amount_reason: null },
        { scheduled_on: '2026-12-10', state: 'upcoming_estimate', entry: null, expected_amount: '120.00', amount_reason: 'winter bill' },
      ],
    })
    const onSaved = vi.fn()
    render(<PlanningView generation="g1" freshness="current" categories={CATEGORIES} onSaved={onSaved} />)
    fireEvent.click(await screen.findByRole('button', { name: /Occurrences/ }))

    const varied = (await screen.findByText('2026-12-10')).closest('tr') as HTMLElement
    expect(within(varied).getByText('SGD 120.00')).toBeInTheDocument()
    expect(within(varied).getByTitle('winter bill')).toBeInTheDocument()
    const usual = screen.getByText('2026-11-10').closest('tr') as HTMLElement
    expect(within(usual).getByText('SGD 80.00')).toBeInTheDocument()
    expect(within(usual).queryByRole('option', { name: 'Expect the usual amount' })).toBeNull()

    fireEvent.change(within(usual).getByLabelText('Occurrence decision'), { target: { value: 'amount' } })
    fireEvent.change(within(usual).getByLabelText('Expected amount'), { target: { value: '95.50' } })
    fireEvent.change(within(usual).getByLabelText('Occurrence reason'), { target: { value: 'rate change letter' } })
    apiMock.mockResolvedValueOnce({})
    fireEvent.click(within(usual).getByRole('button', { name: 'Preview' }))
    apiMock.mockResolvedValueOnce({ status: 'recorded' })
    fireEvent.click(await within(usual).findByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))

    const commit = apiMock.mock.calls.find(c => c[0] === 'commands')?.[1] as { kind: string; anchors: object; payload: object }
    expect(commit.kind).toBe('RECURRING_OCCURRENCE_AMOUNT_SET')
    expect(commit.anchors).toEqual({ commitment: 'c1', scheduled_on: '2026-11-10' })
    expect(commit.payload).toEqual({ amount: '95.50', reason: 'rate change letter' })
  })

  it('freezes edits when the ledger is stale', async () => {
    setup()
    render(<PlanningView generation="g1" freshness="stale" categories={CATEGORIES} onSaved={() => {}} />)
    await screen.findByText('Dining')
    expect(screen.getByText(/Refresh before editing plans/i)).toBeInTheDocument()
    for (const b of screen.getAllByRole('button', { name: /New budget|New goal|Revise|Add progress/ })) {
      expect((b as HTMLButtonElement).disabled).toBe(true)
    }
  })
})
