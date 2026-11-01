import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { PlannedView } from './index'
import { monthlyRows } from './api'

const apiMock = vi.fn()
vi.mock('@/lib/api', async original => ({ ...await original<typeof import('@/lib/api')>(), api: (...args: unknown[]) => apiMock(...args) }))
afterEach(() => { cleanup(); apiMock.mockReset() })

const FLOW = '11111111-1111-4111-8111-111111111111'
const TRIP_FLOW = '22222222-2222-4222-8222-222222222222'
const occurrence = (key: string, earliest: string, latest: string, state: string, extra: Record<string, unknown> = {}) => ({
  key, earliest, latest, on: earliest === latest ? earliest : null, month: earliest.slice(0, 7), planned: '300.00', matched: '0.00', remaining: '300.00',
  state, overdue: state === 'overdue', outcome: null, in_forecast: true, forecast: null, forecast_note: null, matches: [], actual_parts: [], ...extra,
})
const flow = (id: string, extra: Record<string, unknown> = {}) => ({
  flow: id, name: 'Course', direction: 'outflow', currency: 'SGD', account_id: null, account_name: null, category: null,
  certainty: 'sure', baseline_treatment: 'extra', link: null, reason: 'enrolled', planned: '600.00', matched: '0.00', remaining: '600.00',
  over_plan: null, next_due: '2026-01-10',
  occurrences: [
    occurrence('1', '2026-01-10', '2026-01-10', 'overdue', { matches: [{ entry: 'e-old', on: '2026-01-09', amount: '20.00', description: 'Deposit', account: 'Current', dangling: false, reason: 'paid' }], matched: '20.00', remaining: '280.00' }),
    occurrence('2', '2026-03-01', '2026-03-31', 'upcoming'),
  ],
  ...extra,
})
const page = {
  generation: 'g1', as_of: '2026-02-10', cutoff: '2026-01-20', reference_day: '2026-01-20', horizon: { start: '2026-01-21', end: '2026-07-20' },
  flows: [flow(FLOW), flow(TRIP_FLOW, { name: 'Spring trip', link: { kind: 'trip', id: 't1', name: 'Spring trip', exists: true }, occurrences: [occurrence('1', '2026-03-01', '2026-03-31', 'upcoming')] })],
  months: [
    { month: '2026-01', items: [{ flow: FLOW, occurrence: '1' }], totals: { SGD: { outflows: '280.00', inflows: '0.00' } } },
    { month: '2026-03', items: [{ flow: FLOW, occurrence: '2' }, { flow: TRIP_FLOW, occurrence: '1' }], totals: { SGD: { outflows: '600.00', inflows: '0.00' } } },
  ],
  attention: [{ flow: FLOW, occurrence: '1', name: 'Course', direction: 'outflow', currency: 'SGD', latest: '2026-01-10', remaining: '280.00', question: 'Did this payment happen?', trip_linked: false }],
  rules: [],
}
const impact = {
  generation: 'g1', flow: 'draft', currency: 'SGD', flips: true,
  before: { answer: 'Yes', complete: true, reporting_currency: 'SGD', base: { verdict: 'enough', lowest: '80.26', lowest_on: '2026-07-20', first_short_on: null, shortfall: null },
    cautious: { verdict: 'enough', lowest: '80.26', lowest_on: '2026-07-20', first_short_on: null, shortfall: null } },
  after: { answer: 'No', complete: true, reporting_currency: 'SGD', base: { verdict: 'short', lowest: '-1,019.74', lowest_on: '2026-07-20', first_short_on: '2026-12-15', shortfall: '1019.74' },
    cautious: { verdict: 'short', lowest: '-1,019.74', lowest_on: '2026-07-20', first_short_on: '2026-12-15', shortfall: '1019.74' } },
  months: [{ month: '2026-12', currency: 'SGD', base: { before: '80.26', after: '-1119.74', change: '-1200.00' }, cautious: { before: '80.26', after: '-1119.74', change: '-1200.00' } }],
}
const candidates = {
  generation: 'g1', flow: FLOW, occurrence: '1', currency: 'SGD', planned: '300.00', remaining: '280.00', window: { from: '2025-12-27', to: '2026-01-24' },
  candidates: [{ entry: 'e1', on: '2026-01-11', merchant: 'ACADEMY', description: 'Academy', account: 'Current', amount: '280.00', available: '280.00', suggested_amount: '280.00', matched_here: null }],
}

function routes(path: string, body?: unknown) {
  if (path === 'reporting/workspaces/planned') return Promise.resolve(page)
  if (path === 'accounts') return Promise.resolve({ accounts: [{ id: 'a1', display_name: 'Current', currencies: ['SGD'] }] })
  if (path === 'trips') return Promise.resolve({ trips: [{ trip_id: 't1', name: 'Spring trip', start_on: '2026-03-01', end_on: '2026-03-07' }] })
  if (path === 'planned/impact') return Promise.resolve(impact)
  if (path.startsWith('planned/match-candidates')) return Promise.resolve(candidates)
  if (path === 'preview' || path === 'commands' || path === 'batch') return Promise.resolve({ status: 'applied', body })
  return Promise.reject(new Error(`unexpected ${path}`))
}

function sent(path: string) {
  return apiMock.mock.calls.filter(([p]) => p === path).at(-1)![1] as { kind: string; anchors: Record<string, string>; payload: Record<string, unknown>; items?: unknown[] }
}

async function view(onSaved = vi.fn()) {
  apiMock.mockImplementation(routes)
  render(<PlannedView generation="g1" currencies={['SGD', 'USD']} categories={['Shopping / Electronics', 'Internal / Transfer']} onSaved={onSaved}/>)
  await screen.findByText('These dates have passed')
  return onSaved
}

describe('PlannedView', () => {
  it('adds one-off spending through preview, then save', async () => {
    const onSaved = await view()
    fireEvent.click(screen.getByRole('button', { name: /Add planned spending or income/ }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Laptop' } })
    fireEvent.change(screen.getByLabelText('Date'), { target: { value: '2026-12-15' } })
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '1200' } })
    fireEvent.change(screen.getByLabelText('Compared with usual spending'), { target: { value: 'irregular_allowance' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Old one is dying' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview planned item' }))
    await screen.findByRole('button', { name: 'Save planned item' })
    const preview = sent('preview')
    expect(preview.kind).toBe('PLANNED_FLOW_SET')
    expect(preview.payload).toEqual({
      name: 'Laptop', direction: 'outflow', currency: 'SGD', account_id: null, category: null,
      occurrences: [{ key: '1', earliest: '2026-12-15', latest: '2026-12-15', amount: '1200' }],
      certainty: 'sure', baseline_treatment: 'irregular_allowance', link: null, reason: 'Old one is dying',
    })
    expect(screen.getAllByRole('status')[0]).toHaveTextContent(/going out: 1,200.00 SGD \(.*2026\)/)
    fireEvent.click(screen.getByRole('button', { name: 'Save planned item' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
  })

  it('shows what the draft does to the answer before it can be saved', async () => {
    await view()
    fireEvent.click(screen.getByRole('button', { name: /Add planned spending or income/ }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Laptop' } })
    fireEvent.change(screen.getByLabelText('Date'), { target: { value: '2026-12-15' } })
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '1200' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Old one is dying' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview planned item' }))
    const effect = (await screen.findByText(/with this — this changes the answer/)).closest('.planned-impact') as HTMLElement
    expect(effect).toHaveTextContent('Yes before, No with this')
    expect(within(effect).getByText(/Base case: lowest point 80.26 SGD on .* before, −?1,019.74 SGD/)).toBeInTheDocument()
    expect(within(effect).getByText('December 2026')).toBeInTheDocument()
    expect(within(effect).getAllByText('−1,200.00 SGD')).toHaveLength(2)
    expect(sent('planned/impact')).toMatchObject({ flow: expect.any(String), payload: { name: 'Laptop', direction: 'outflow' } })
  })

  it('offers the trip’s dates as the window when a trip is linked', async () => {
    await view()
    fireEvent.click(screen.getByRole('button', { name: /Add planned spending or income/ }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Spring trip food' } })
    fireEvent.change(await screen.findByLabelText('Trip'), { target: { value: 't1' } })
    expect(screen.getByLabelText('When')).toHaveValue('trip')
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '500' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Budget' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview planned item' }))
    await screen.findByRole('button', { name: 'Save planned item' })
    expect(sent('preview').payload).toMatchObject({
      occurrences: [{ key: '1', earliest: '2026-03-01', latest: '2026-03-07', amount: '500' }], link: { kind: 'trip', id: 't1' },
    })
    fireEvent.change(screen.getByLabelText('Trip'), { target: { value: '' } })
    expect(screen.getByLabelText('When')).toHaveValue('day')
  })

  it('lists a monthly series as editable dates and income is always extra', async () => {
    await view()
    fireEvent.click(screen.getByRole('button', { name: /Add planned spending or income/ }))
    fireEvent.click(screen.getByLabelText(/Coming in/))
    expect(screen.queryByLabelText('Compared with usual spending')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Tutoring' } })
    fireEvent.change(screen.getByLabelText('When'), { target: { value: 'monthly' } })
    fireEvent.change(screen.getByLabelText('First date'), { target: { value: '2026-01-31' } })
    fireEvent.change(screen.getByLabelText('How many months'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('Amount'), { target: { value: '150.00' } })
    fireEvent.click(screen.getByRole('button', { name: 'List the months' }))
    fireEvent.change(screen.getByLabelText('Amount 2'), { target: { value: '175.50' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Agreed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview planned item' }))
    await screen.findByRole('button', { name: 'Save planned item' })
    expect(sent('preview').payload).toMatchObject({
      direction: 'inflow', baseline_treatment: 'extra', link: null,
      occurrences: [{ key: '1', earliest: '2026-01-31', latest: '2026-01-31', amount: '150.00' }, { key: '2', earliest: '2026-02-28', latest: '2026-02-28', amount: '175.50' }],
    })
  })

  it('asks about a passed date and records that it did not happen', async () => {
    await view()
    const attention = screen.getByText('These dates have passed').closest('.planned-attention') as HTMLElement
    expect(within(attention).getByText('Did this payment happen?')).toBeInTheDocument()
    // A date with a matched payment can be marked done but not as not happening.
    expect(within(attention).queryByRole('button', { name: 'Didn’t happen' })).not.toBeInTheDocument()
    fireEvent.click(within(attention).getByRole('button', { name: 'Done' }))
    fireEvent.change(within(attention).getByLabelText('Reason'), { target: { value: 'Rest was waived' } })
    fireEvent.click(within(attention).getByRole('button', { name: 'Mark done' }))
    await within(attention).findByRole('button', { name: 'Save: mark done' })
    expect(sent('preview')).toMatchObject({ kind: 'PLANNED_OCCURRENCE_RESOLVE_SET', anchors: { flow: FLOW, occurrence: '1' }, payload: { outcome: 'done', reason: 'Rest was waived' } })
  })

  it('matches a payment using the amount the server suggests', async () => {
    await view()
    const attention = screen.getByText('These dates have passed').closest('.planned-attention') as HTMLElement
    fireEvent.click(within(attention).getByRole('button', { name: 'Match a payment' }))
    fireEvent.click(await within(attention).findByRole('radio'))
    expect(within(attention).getByLabelText('Amount to count')).toHaveValue('280.00')
    fireEvent.change(within(attention).getByLabelText('Reason'), { target: { value: 'Invoice paid' } })
    fireEvent.click(within(attention).getByRole('button', { name: 'Preview match' }))
    await within(attention).findByRole('button', { name: 'Save match' })
    expect(sent('preview')).toMatchObject({ kind: 'PLANNED_MATCH_SET', anchors: { flow: FLOW, occurrence: '1', entry: 'e1' }, payload: { amount: '280.00', reason: 'Invoice paid' } })
  })

  it('never offers manual matching for a trip budget and removes a flow with its matches', async () => {
    await view()
    const trip = screen.getAllByText('Spring trip', { selector: 'strong' })[0].closest('.planned-row') as HTMLElement
    expect(within(trip).queryByRole('button', { name: 'Match a payment' })).not.toBeInTheDocument()
    const course = screen.getByText('These dates have passed').closest('.planned-attention') as HTMLElement
    fireEvent.click(within(course).getByRole('button', { name: 'Remove item' }))
    fireEvent.change(within(course).getByLabelText('Reason'), { target: { value: 'Dropped out' } })
    fireEvent.click(within(course).getByRole('button', { name: 'Remove planned item' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('batch', expect.anything()))
    expect(sent('batch').items).toEqual([
      { kind: 'PLANNED_MATCH_RETIRE', anchors: { flow: FLOW, occurrence: '1', entry: 'e-old' }, payload: { reason: 'Dropped out' } },
      { kind: 'PLANNED_FLOW_RETIRE', anchors: { flow: FLOW }, payload: { reason: 'Dropped out' } },
    ])
  })
})

describe('monthlyRows', () => {
  it('keeps the day and clamps it to short months', () => {
    expect(monthlyRows('2026-11-30', 4, '10').map(row => row.earliest)).toEqual(['2026-11-30', '2026-12-30', '2027-01-30', '2027-02-28'])
  })
})
