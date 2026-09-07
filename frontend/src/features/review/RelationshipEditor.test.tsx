import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const review = {
  generation: 'sha256:abc',
  kind: 'duplicate',
  proposals: [{
    primary: 'anchor-a', candidate: 'anchor-b', account: 'Revolut', currency: 'SGD',
    amount: '12.00', date: '2026-01-05', merchant: 'ALPHA', event_kind: 'PURCHASE',
    basis: 'Same account, amount, date and merchant across two imports',
  }],
  next_cursor: null,
  total: 1,
}

const apiMock = vi.fn(async (path: string, body?: unknown) => {
  if (path.startsWith('relationships/review')) return review
  if (path === 'preview') return { valid: true }
  if (path === 'commands') return { status: 'applied' }
  throw new Error(`unmocked ${path} ${JSON.stringify(body)}`)
})
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

async function mount() {
  const { RelationshipEditor } = await import('./RelationshipEditor')
  return render(
    <RelationshipEditor generation="sha256:abc" freshness="current" onSaved={() => {}} />,
  )
}

afterEach(() => { cleanup(); apiMock.mockClear() })

describe('RelationshipEditor', () => {
  it('lists a cross-import duplicate proposal', async () => {
    await mount()
    expect(await screen.findByText('1 pair across imports')).toBeInTheDocument()
    expect(screen.getByText('anchor-a')).toBeInTheDocument()
    expect(screen.getByText('anchor-b')).toBeInTheDocument()
    expect(screen.getByText(/Same account, amount, date and merchant/)).toBeInTheDocument()
  })

  it('previews then commits a DUPLICATE_CONFIRM', async () => {
    await mount()
    await screen.findByText('1 pair across imports')
    fireEvent.change(screen.getByPlaceholderText(/Why is this the right call/), {
      target: { value: 'same purchase' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Confirm duplicate/ }))
    await waitFor(() => {
      const kinds = apiMock.mock.calls
        .filter(c => c[0] === 'commands')
        .map(c => (c[1] as { kind: string }).kind)
      expect(kinds).toContain('DUPLICATE_CONFIRM')
    })
  })
})
