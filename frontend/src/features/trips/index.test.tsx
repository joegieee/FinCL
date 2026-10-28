import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { TripsView } from './index'

const apiMock = vi.fn()
vi.mock('@/lib/api', async original => ({ ...await original<typeof import('@/lib/api')>(), api: (...args: unknown[]) => apiMock(...args) }))
afterEach(() => { cleanup(); apiMock.mockReset() })

const envelope = { generation: 'g1', lens: 'recorded_flow', lens_note: 'Recorded lens.', country_list_version: 'v1' }
const trip = { trip_id: '11111111-1111-4111-8111-111111111111', name: 'Trip A', start_on: '2026-01-10', end_on: '2026-01-15', status: 'past', countries: ['FR'], reason: 'r', entry_count: 1, review_count: 1, totals_by_currency: { SGD: { gross_cost: '30.00', refunds: '0.00', net_cost: '30.00', entry_count: 1 } } }
const row = (anchor: string, merchant: string, assignment: string, country: string | null) => ({ anchor, on: '2026-01-11', end: '2026-01-11', date_precision: 'DAY', merchant, category_key: 'Travel', account: 'a', currency: 'SGD', cost_amount: '30.00', refund_amount: '0.00', country, country_basis: country ? 'operator' : 'unknown', assignment, candidates: [], categories: [{ category_key: 'Travel', cost_amount: '30.00', refund_amount: '0.00' }] })

function routes(path: string, body?: unknown) {
  if (path === 'reporting/workspaces/trips') return Promise.resolve({ ...envelope, trips: [trip] })
  if (path === `reporting/workspaces/trips/${trip.trip_id}`) return Promise.resolve({ ...envelope, trip, by_category: [{ currency: 'SGD', category_key: 'Travel', cost_amount: '30.00' }],
    contributors: [row('a1', 'Meal', 'automatic', 'FR')], review: { missing_country: [row('a2', 'Lodging', 'none', null)], ambiguous: [], other_country: [], excluded: [] },
    manual_assignments: [{ anchor: 'a9', on: '2025-12-20', counted: false }] })
  if (path === 'countries') return Promise.resolve({ version: 'v1', codes: ['FR', 'DE', 'SG'] })
  if (path === 'preview' || path === 'commands' || path === 'batch') return Promise.resolve({ status: 'applied', body })
  return Promise.reject(new Error(`unexpected ${path}`))
}

describe('TripsView', () => {
  it('creates a trip through preview, then save, with native dates and country codes', async () => {
    apiMock.mockImplementation(routes)
    const onSaved = vi.fn()
    render(<TripsView generation="g1" onSaved={onSaved}/>)
    fireEvent.click(await screen.findByRole('button', { name: /New trip/ }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Trip B' } })
    fireEvent.change(screen.getByLabelText('First day'), { target: { value: '2026-01-20' } })
    fireEvent.change(screen.getByLabelText('Last day'), { target: { value: '2026-01-24' } })
    fireEvent.change(screen.getByLabelText('Countries'), { target: { value: 'de' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Holiday' } })
    fireEvent.click(screen.getByRole('button', { name: 'Preview trip' }))
    await screen.findByRole('button', { name: 'Save trip' })
    const preview = apiMock.mock.calls.find(([path]) => path === 'preview')![1] as { kind: string; payload: Record<string, unknown> }
    expect(preview.kind).toBe('TRIP_SET')
    expect(preview.payload).toEqual({ name: 'Trip B', start_on: '2026-01-20', end_on: '2026-01-24', countries: ['DE'], reason: 'Holiday' })
    fireEvent.click(screen.getByRole('button', { name: 'Save trip' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
  })

  it('refuses an unknown country code before anything is sent', async () => {
    apiMock.mockImplementation(routes)
    render(<TripsView generation="g1" onSaved={() => {}}/>)
    fireEvent.click(await screen.findByRole('button', { name: /New trip/ }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('countries'))
    fireEvent.change(screen.getByLabelText('Countries'), { target: { value: 'ZZ' } })
    expect(await screen.findByText(/Not a country code: ZZ/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Preview trip' })).toBeDisabled()
  })

  it('reviews then saves one batch that sets the country on the selected rows', async () => {
    apiMock.mockImplementation(routes)
    render(<TripsView generation="g1" onSaved={() => {}}/>)
    fireEvent.click(await screen.findByRole('button', { name: /Trip A/ }))
    fireEvent.click(await screen.findByLabelText('Select Lodging on 2026-01-11'))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Receipt shows the city' } })
    fireEvent.click(screen.getByRole('button', { name: 'Review changes' }))
    expect(screen.getByRole('status')).toHaveTextContent('Set country to FR (France) on 1 transaction: Lodging')
    expect(apiMock).not.toHaveBeenCalledWith('batch', expect.anything())
    fireEvent.click(screen.getByRole('button', { name: 'Save 1 change' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('batch', expect.anything()))
    const body = apiMock.mock.calls.find(([path]) => path === 'batch')![1] as { items: unknown[] }
    expect(body.items).toEqual([{ kind: 'ENTRY_COUNTRY_SET', anchors: { entry: 'a2' }, payload: { country: 'FR', reason: 'Receipt shows the city' } }])
  })

  it('removes a trip together with every manual assignment the server lists, counted or not', async () => {
    apiMock.mockImplementation(routes)
    render(<TripsView generation="g1" onSaved={() => {}}/>)
    fireEvent.click(await screen.findByRole('button', { name: /Trip A/ }))
    expect(await screen.findByText(/including ones no longer counted as trip cost/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove trip' }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('batch', expect.anything()))
    const body = apiMock.mock.calls.find(([path]) => path === 'batch')![1] as { items: { kind: string; anchors: Record<string, string> }[] }
    expect(body.items.map(item => [item.kind, item.anchors])).toEqual([['ENTRY_TRIP_RETIRE', { entry: 'a9' }], ['TRIP_RETIRE', { trip: trip.trip_id }]])
  })

  it("shows each trip's status and an empty budget slot on the detail", async () => {
    apiMock.mockImplementation(routes)
    render(<TripsView generation="g1" onSaved={() => {}}/>)
    expect(await screen.findByText('Past')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Trip A/ }))
    expect(await screen.findByText(/· PAST/)).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Budget' })).toHaveTextContent('No budget for this trip yet.')
  })
})
