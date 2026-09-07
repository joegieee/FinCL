import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const apiMock = vi.fn()
vi.mock('@/lib/api', () => ({
  api: (path: string, body?: unknown) => apiMock(path, body),
  ApiError: class ApiError extends Error { status: number; constructor(m: string, s: number) { super(m); this.status = s } },
}))

const { BulkAssign } = await import('./BulkAssign')

const CATEGORIES = ['Food & Dining / Groceries', 'Lifestyle / Books', 'Internal / Transfer']
const FILTERS = { start: '2026-01-01', end: '2026-01-31', currency: 'SGD', search: '', category: '', account: '', kind: 'spending', include_excluded: false }

function base(over: Record<string, unknown> = {}) {
  return {
    generation: 'g1', freshness: 'current', categories: CATEGORIES,
    pageAnchors: ['a1', 'a2'], picked: new Set(['a1', 'a2']), matchAll: null as null | { anchors: string[]; truncated: boolean },
    total: 10, filters: FILTERS,
    onSelectMatch: vi.fn(), onClear: vi.fn(), onSaved: vi.fn(),
    ...over,
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('BulkAssign', () => {
  it('commits an explicit selection as one CATEGORY_OVERRIDE batch', async () => {
    const props = base()
    render(<BulkAssign {...props} />)
    fireEvent.change(screen.getByLabelText('Set category'), { target: { value: 'Lifestyle / Books' } })
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'reclassify' } })

    apiMock.mockResolvedValueOnce({ status: 'applied' })
    fireEvent.click(screen.getByRole('button', { name: /Apply to 2/ }))
    await waitFor(() => expect(props.onSaved).toHaveBeenCalledWith('applied'))
    expect(props.onClear).toHaveBeenCalled()

    const [path, body] = apiMock.mock.calls[0] as [string, { items: { kind: string; anchors: { entry: string }; payload: { category: string } }[] }]
    expect(path).toBe('batch')
    expect(body.items).toHaveLength(2)
    expect(body.items[0].kind).toBe('CATEGORY_OVERRIDE')
    expect(body.items[0].payload.category).toBe('Lifestyle / Books')
  })

  it('materializes "all matching", then commits in 200-item chunks with distinct request ids', async () => {
    const anchors = Array.from({ length: 450 }, (_, i) => `x${i}`)
    const onSelectMatch = vi.fn()
    const { rerender } = render(<BulkAssign {...base({ picked: new Set(['x0']), onSelectMatch })} />)

    apiMock.mockResolvedValueOnce({ anchors, truncated: false })
    fireEvent.click(screen.getByRole('button', { name: /Select all 10 matching/ }))
    await waitFor(() => expect(onSelectMatch).toHaveBeenCalledWith({ anchors, truncated: false }))
    expect(apiMock.mock.calls[0][0]).toBe('transactions/anchors?generation=g1&currency=SGD&start=2026-01-01&end=2026-01-31&kind=spending')

    rerender(<BulkAssign {...base({ matchAll: { anchors, truncated: false }, onSelectMatch })} />)
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'bulk' } })
    apiMock.mockResolvedValue({ status: 'applied' })
    fireEvent.click(screen.getByRole('button', { name: /Apply to 450 in 3 batches/ }))

    await waitFor(() => expect(apiMock.mock.calls.filter(c => c[0] === 'batch')).toHaveLength(3))
    const batchCalls = apiMock.mock.calls.filter(c => c[0] === 'batch') as [string, { request_id: string; items: unknown[] }][]
    expect(batchCalls.map(c => c[1].items.length)).toEqual([200, 200, 50])
    expect(new Set(batchCalls.map(c => c[1].request_id)).size).toBe(3)
  })

  it('freezes when the ledger is stale', () => {
    render(<BulkAssign {...base({ freshness: 'stale' })} />)
    expect(screen.getByText(/Refresh the ledger before a bulk correction/)).toBeInTheDocument()
    expect((screen.getByRole('button', { name: /Apply to 2/ }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('keeps the draft and reports committed chunks on a mid-run failure', async () => {
    const anchors = Array.from({ length: 300 }, (_, i) => `y${i}`)
    render(<BulkAssign {...base({ matchAll: { anchors, truncated: false } })} />)
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'bulk' } })

    apiMock.mockResolvedValueOnce({ status: 'applied' })
    apiMock.mockRejectedValueOnce(new Error('generation moved'))
    fireEvent.click(screen.getByRole('button', { name: /Apply to 300 in 2 batches/ }))

    expect(await screen.findByText(/1 of 2 batches were already applied/)).toBeInTheDocument()
    expect((screen.getByLabelText('Reason') as HTMLInputElement).value).toBe('bulk')
  })
})
