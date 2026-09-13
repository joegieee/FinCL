import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { AllocationInput } from './api'

const page = (candidates: unknown[]) => ({
  generation: 'sha256:abc', debt_id: 'd-1', event_kind: 'LENT',
  required_direction: 'OUT', currency: 'SGD', next_cursor: null, candidates,
})

const initialPage = page([
  { entry_anchor: 'anchor-1', effective_at: '2026-03-05T10:00:00', economic_direction: 'OUT',
    currency: 'SGD', amount: '19.00', open_amount: '19.00', description: 'Bookshop' },
])

const searchPage = page([
  { entry_anchor: 'anchor-9', effective_at: '2026-01-02T10:00:00', economic_direction: 'OUT',
    currency: 'SGD', amount: '4.00', open_amount: '4.00', description: 'Cafe Social lunch' },
])

const calls: string[] = []
const apiMock = vi.fn(async (path: string) => {
  calls.push(path)
  if (path.includes('search=')) return searchPage
  if (path.startsWith('reporting/workspaces/debts/allocation-candidates')) return initialPage
  throw new Error(`unmocked ${path}`)
})
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string) => apiMock(path) }
})

afterEach(() => { cleanup(); apiMock.mockClear(); calls.length = 0 })

function StatefulTable({
  Table, initialRows,
}: {
  Table: typeof import('./AllocationTable').AllocationTable
  initialRows: AllocationInput[]
}) {
  const [rows, setRows] = useState(initialRows)
  return (
    <Table
      debtId="d-1" eventKind="LENT" generation="sha256:abc" currency="SGD"
      rows={rows} onChange={setRows} eventPrincipal="4.00"
    />
  )
}

describe('AllocationTable source search', () => {
  it('debounces a query into a server-side search beyond the initial page', async () => {
    const { AllocationTable, emptyAllocation } = await import('./AllocationTable')
    render(
      <AllocationTable
        debtId="d-1" eventKind="LENT" generation="sha256:abc" currency="SGD"
        rows={[emptyAllocation()]} onChange={vi.fn()} eventPrincipal="4.00"
      />,
    )

    await screen.findByLabelText('Allocation 1 source')
    fireEvent.change(screen.getByLabelText('Allocation 1 source'), { target: { value: 'kopi' } })

    // not in the first page, so it only surfaces once the debounced search resolves
    expect(screen.queryByText('Cafe Social lunch')).not.toBeInTheDocument()
    await screen.findByText('Cafe Social lunch')
    expect(calls.some((c) => c.includes('search=kopi'))).toBe(true)

    fireEvent.click(screen.getByText('Cafe Social lunch'))
    await waitFor(() => expect(screen.getByLabelText('Allocation 1 source')).toHaveValue('Cafe Social lunch'))
  })

  it('fills the principal field with the transaction amount on selection', async () => {
    const { AllocationTable, emptyAllocation } = await import('./AllocationTable')
    render(<StatefulTable Table={AllocationTable} initialRows={[emptyAllocation()]} />)

    await screen.findByLabelText('Allocation 1 source')
    fireEvent.focus(screen.getByLabelText('Allocation 1 source'))
    await screen.findByText('Bookshop')
    fireEvent.click(screen.getByText('Bookshop'))

    await waitFor(() => expect(screen.getByLabelText('Allocation 1 principal')).toHaveValue('19.00'))
  })
})
