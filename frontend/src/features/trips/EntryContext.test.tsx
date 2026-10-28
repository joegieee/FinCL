import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { EntryContextSection } from './EntryContext'

const apiMock = vi.fn()
vi.mock('@/lib/api', async original => ({ ...await original<typeof import('@/lib/api')>(), api: (...args: unknown[]) => apiMock(...args) }))
afterEach(() => { cleanup(); apiMock.mockReset() })

const tripA = { trip_id: '11111111-1111-4111-8111-111111111111', name: 'Trip A', start_on: '2026-01-10', end_on: '2026-01-15', countries: ['FR'], in_window: true }
const context = (over: object = {}) => ({ generation: 'g1', anchor: 'a1', counted_as_cost: true, on: '2026-01-11', end: '2026-01-11', country: null, country_basis: 'unknown', trip: null, assignment: 'none', candidates: [], trips: [tripA], ...over })

function routes(body: object) {
  return (path: string, sent?: unknown) => {
    if (path === 'entry-context?anchor=a1') return Promise.resolve(body)
    if (path === 'countries') return Promise.resolve({ version: 'v1', codes: ['FR', 'JP'] })
    if (path === 'batch') return Promise.resolve({ status: 'applied', body: sent })
    return Promise.reject(new Error(`unexpected ${path}`))
  }
}

describe('EntryContextSection', () => {
  it('reviews then saves the country and the trip as one batch', async () => {
    apiMock.mockImplementation(routes(context()))
    const onSaved = vi.fn(async () => {})
    render(<EntryContextSection anchor="a1" generation="g1" disabled={false} onSaved={onSaved}/>)
    expect(await screen.findByText(/Country: unknown\. Trip: not in a trip\./)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('option', { name: 'FR · France' })).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Country'), { target: { value: 'FR' } })
    fireEvent.change(screen.getByLabelText('Trip'), { target: { value: tripA.trip_id } })
    fireEvent.change(screen.getByLabelText('Why this trip or country'), { target: { value: 'Paid in Paris' } })
    fireEvent.click(screen.getByRole('button', { name: /Review trip and country/ }))
    expect(screen.getByRole('status')).toHaveTextContent('Sets the country to France. Assigns it to Trip A.')
    expect(apiMock).not.toHaveBeenCalledWith('batch', expect.anything())
    fireEvent.click(screen.getByRole('button', { name: /Save trip and country/ }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('applied'))
    const body = apiMock.mock.calls.find(([path]) => path === 'batch')![1] as { items: unknown[] }
    expect(body.items).toEqual([
      { kind: 'ENTRY_COUNTRY_SET', anchors: { entry: 'a1' }, payload: { country: 'FR', reason: 'Paid in Paris' } },
      { kind: 'ENTRY_TRIP_SET', anchors: { entry: 'a1' }, payload: { trip: tripA.trip_id, reason: 'Paid in Paris' } },
    ])
  })

  it('returns a manual choice to automatic and clears a country the user set', async () => {
    apiMock.mockImplementation(routes(context({ country: 'FR', country_basis: 'operator', trip: tripA.trip_id, assignment: 'manual' })))
    render(<EntryContextSection anchor="a1" generation="g1" disabled={false} onSaved={async () => {}}/>)
    expect(await screen.findByText(/Trip A, chosen by hand/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Country'), { target: { value: 'source' } })
    fireEvent.change(screen.getByLabelText('Trip'), { target: { value: 'automatic' } })
    fireEvent.change(screen.getByLabelText('Why this trip or country'), { target: { value: 'Undo' } })
    fireEvent.click(screen.getByRole('button', { name: /Review trip and country/ }))
    fireEvent.click(screen.getByRole('button', { name: /Save trip and country/ }))
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('batch', expect.anything()))
    const body = apiMock.mock.calls.find(([path]) => path === 'batch')![1] as { items: { kind: string }[] }
    expect(body.items.map(item => item.kind)).toEqual(['ENTRY_COUNTRY_RETIRE', 'ENTRY_TRIP_RETIRE'])
  })

  it('offers no trip to a transaction that is not spending or a refund', async () => {
    apiMock.mockImplementation(routes(context({ counted_as_cost: false })))
    render(<EntryContextSection anchor="a1" generation="g1" disabled={false} onSaved={async () => {}}/>)
    expect(await screen.findByText(/never counts toward a trip/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Trip')).not.toBeInTheDocument()
  })
})
