import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

const apiMock = vi.fn()
vi.mock('@/lib/api', () => ({
  api: (path: string, body?: unknown) => apiMock(path, body),
  ApiError: class ApiError extends Error { status: number; constructor(m: string, s: number) { super(m); this.status = s } },
}))

const { ReviewHub } = await import('./ReviewHub')

const REVIEW = {
  generation: 'g1',
  issues: [
    { queue: 'source', issue_key: 'extraction:1', status: 'BLOCKING', title: 'Unreadable row', detail: 'line 4', owner: 'run-1', fingerprint: 'fp-1', deferred: false },
    { queue: 'categories', issue_key: 'entry:a1', status: 'review', title: 'Category needs review', detail: 'ambiguous', owner: 'a1', fingerprint: 'fp-2', deferred: false },
    { queue: 'categories', issue_key: 'entry:a2', status: 'review', title: 'Category needs review', detail: 'conflict', owner: 'a2', fingerprint: 'fp-3', deferred: true },
  ],
}

function routeMock() {
  apiMock.mockImplementation((path: string) => {
    if (path === 'review') return Promise.resolve(REVIEW)
    if (path === 'history') return Promise.resolve([{ sequence: 7, date: '2026-02-01', kind: 'MERCHANT_ALIAS_SET', reason: 'normalize', category: '' }])
    return Promise.resolve({})
  })
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('ReviewHub', () => {
  it('groups issues into independent per-queue counts and never sums them', async () => {
    routeMock()
    render(<ReviewHub freshness="current" onSaved={() => {}} />)
    expect(await screen.findByText('Source & coverage: 1')).toBeInTheDocument()
    expect(screen.getByText('Categories & inflows: 1')).toBeInTheDocument()
    expect(screen.getByText(/never added together/)).toBeInTheDocument()
    // The one deferred issue is not shown in the active queue.
    expect(screen.getByText(/Show 1 deferred/)).toBeInTheDocument()
  })

  it('defers an issue through preview then commit with its fingerprint', async () => {
    routeMock()
    const onSaved = vi.fn()
    render(<ReviewHub freshness="current" onSaved={onSaved} />)
    await screen.findByText('Unreadable row')

    const sourceCard = within(screen.getByText('SOURCE & COVERAGE').closest('.review-queue') as HTMLElement)
    fireEvent.click(sourceCard.getByRole('button', { name: 'Later' }))
    await sourceCard.findByRole('button', { name: 'Confirm' })
    fireEvent.click(sourceCard.getByRole('button', { name: 'Confirm' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())

    const commit = apiMock.mock.calls.find(c => c[0] === 'commands')?.[1] as { kind: string; payload: { fingerprint: string } }
    expect(commit.kind).toBe('REVIEW_DEFER_SET')
    expect(commit.payload.fingerprint).toBe('fp-1')
  })

  it('previews an unsupported inverse without offering to apply it', async () => {
    apiMock.mockImplementation((path: string) => {
      if (path === 'review') return Promise.resolve(REVIEW)
      if (path === 'history') return Promise.resolve([{ sequence: 7, date: '2026-02-01', kind: 'INBOUND_CLASSIFICATION', reason: 'x', category: '' }])
      if (path.startsWith('history/7/inverse-preview')) return Promise.resolve({ supported: false, reason: 'This decision has no safe automatic inverse' })
      return Promise.resolve({})
    })
    render(<ReviewHub freshness="current" onSaved={() => {}} />)
    await screen.findByText('Unreadable row')
    // The decision <select> is populated by an async history fetch — wait for it.
    await screen.findByRole('option', { name: /#7/ })
    fireEvent.change(screen.getByLabelText('Decision'), { target: { value: '7' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check inverse' }))
    expect(await screen.findByText(/no safe automatic inverse/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Apply inverse/ })).toBeNull()
  })
})
