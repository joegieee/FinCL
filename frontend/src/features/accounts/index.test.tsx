import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

const apiMock = vi.fn()
vi.mock('@/lib/api', () => ({
  api: (path: string, body?: unknown) => apiMock(path, body),
  ApiError: class ApiError extends Error { status: number; constructor(m: string, s: number) { super(m); this.status = s } },
}))

const loadAccounts = vi.fn()
vi.mock('./api', () => ({ loadAccounts: () => loadAccounts() }))

const { AccountsView } = await import('./index')

function page(over: Record<string, unknown> = {}) {
  return {
    generation: 'g1',
    accounts: [{
      id: 'acc-1', institution: 'Revolut', source_type: 'CSV', display_name: 'Revolut Current',
      masked_identifier: '••1234', archived_on: '', entry_count: 3, currencies: ['SGD'],
      pockets: [{ pocket: 'SGD', expected_start: null, expected_end: null, observed_start: '2026-01-01', observed_end: '2026-01-20', entry_count: 3, coverage_status: 'unconfigured', gaps: [] }],
      coverage_status: 'unconfigured', expected_start: null, expected_end: null,
    }],
    aliases: [],
    global_coverage: { covered: false, reasons: ['Economic-flow report unavailable for 2026-01'] },
    ...over,
  }
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('AccountsView', () => {
  it('lists per-pocket coverage and the reports affected by coverage', async () => {
    loadAccounts.mockResolvedValue(page())
    render(<AccountsView freshness="current" onSaved={() => {}} />)
    expect(await screen.findByRole('heading', { name: 'Revolut Current' })).toBeInTheDocument()
    expect(screen.getByText(/Economic-flow report unavailable/)).toBeInTheDocument()
    expect(screen.getByText('2026-01-01 → 2026-01-20')).toBeInTheDocument()
  })

  it('names the days between statements that no statement covers', async () => {
    const gapped = page()
    gapped.accounts[0].pockets = [{
      pocket: 'SGD', expected_start: null, expected_end: null, observed_start: '2026-01-01',
      observed_end: '2026-03-31', entry_count: 5, coverage_status: 'gap',
      gaps: [{ start: '2026-02-01', end: '2026-02-28' }],
    }]
    loadAccounts.mockResolvedValue(gapped)
    render(<AccountsView freshness="current" onSaved={() => {}} />)
    expect(await screen.findByText('No statement for 2026-02-01 → 2026-02-28')).toBeInTheDocument()
  })

  it('previews then commits an expected window with the same request id', async () => {
    loadAccounts.mockResolvedValue(page())
    const onSaved = vi.fn()
    render(<AccountsView freshness="current" onSaved={onSaved} />)
    await screen.findByRole('heading', { name: 'Revolut Current' })

    fireEvent.click(screen.getByRole('button', { name: 'Set window' }))
    const form = within(document.querySelector('.pocket-form-row') as HTMLElement)
    fireEvent.change(form.getByLabelText(/Expected start/), { target: { value: '2026-01-01' } })
    fireEvent.change(form.getByLabelText(/Expected end/), { target: { value: '2026-01-31' } })
    fireEvent.change(form.getByLabelText(/Reason/), { target: { value: 'statement window' } })

    apiMock.mockResolvedValueOnce({}) // preview
    fireEvent.click(form.getByRole('button', { name: 'Preview' }))
    await screen.findByText(/No transactions are created/)

    apiMock.mockResolvedValueOnce({ status: 'recorded' }) // commit
    fireEvent.click(screen.getByRole('button', { name: 'Save window' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('recorded'))

    const previewBody = apiMock.mock.calls[0][1] as { request_id: string; kind: string }
    const commitBody = apiMock.mock.calls[1][1] as { request_id: string }
    expect(previewBody.kind).toBe('ACCOUNT_SCOPE_SET')
    expect(commitBody.request_id).toBe(previewBody.request_id)
  })

  it('freezes writes when the ledger is stale', async () => {
    loadAccounts.mockResolvedValue(page())
    render(<AccountsView freshness="stale" onSaved={() => {}} />)
    await screen.findByRole('heading', { name: 'Revolut Current' })
    fireEvent.click(screen.getByRole('button', { name: 'Set window' }))
    const previews = screen.getAllByRole('button', { name: 'Preview' }) as HTMLButtonElement[]
    expect(previews.every(b => b.disabled)).toBe(true)
  })
})
