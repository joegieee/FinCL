import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const apiMock = vi.fn()
vi.mock('@/lib/api', () => ({
  api: (path: string, body?: unknown) => apiMock(path, body),
  ApiError: class ApiError extends Error { status: number; constructor(m: string, s: number) { super(m); this.status = s } },
}))

const { CustomCategoryManager } = await import('./CustomCategoryManager')

const PAGE = {
  generation: 'g1',
  categories: [{ key: 'Lifestyle / Board Games', display_group: 'Lifestyle', display_name: 'Tabletop', archived: false, active_rule_count: 2, custom: true }],
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('CustomCategoryManager', () => {
  it('archives a custom category through preview then commit', async () => {
    apiMock.mockResolvedValueOnce(PAGE) // initial categories load
    const onSaved = vi.fn()
    render(<CustomCategoryManager freshness="current" onSaved={onSaved} />)
    expect(await screen.findByText('Lifestyle / Tabletop')).toBeInTheDocument()

    expect(screen.getByText('2 active rules')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Archive/ }))
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'not in use' } })

    apiMock.mockResolvedValueOnce({}) // preview
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    expect(await screen.findByText(/its 2 rules keep applying until retired/)).toBeInTheDocument()

    apiMock.mockResolvedValueOnce({ status: 'recorded' }) // commit
    apiMock.mockResolvedValueOnce(PAGE) // reload
    fireEvent.click(screen.getByRole('button', { name: 'Archive' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))

    const commit = apiMock.mock.calls.find(c => c[0] === 'commands')?.[1] as { kind: string; payload: { archived: boolean } }
    expect(commit.kind).toBe('CATEGORY_ARCHIVE_SET')
    expect(commit.payload.archived).toBe(true)
  })

  it('freezes edits when the ledger is stale', async () => {
    apiMock.mockResolvedValueOnce(PAGE)
    render(<CustomCategoryManager freshness="stale" onSaved={() => {}} />)
    await screen.findByText('Lifestyle / Tabletop')
    expect((screen.getByRole('button', { name: /Rename/ }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByText(/Refresh the ledger before editing categories/)).toBeInTheDocument()
  })
})
