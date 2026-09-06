import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ApiError } from '@/lib/api'

const loadSetup = vi.fn()
const loadImports = vi.fn()
const loadImportDetail = vi.fn()
const submitImport = vi.fn()

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    loadSetup: () => loadSetup(),
    loadImports: () => loadImports(),
    loadImportDetail: (id: string) => loadImportDetail(id),
    submitImport: (adapter: string, files: File[]) => submitImport(adapter, files),
  }
})

const { SetupView } = await import('./index')

const SETUP = {
  state: 'ready', current_ledger: 'ledger.sqlite3', storage_profile: 'plaintext-local',
  workspace_root: '/home/user/fincl', can_create: true, can_open: true,
}

afterEach(() => { cleanup(); vi.clearAllMocks() })

describe('SetupView', () => {
  it('shows setup state and an empty import history', async () => {
    loadSetup.mockResolvedValue(SETUP)
    loadImports.mockResolvedValue({ generation: 'g1', imports: [] })
    render(<SetupView onImported={() => {}} />)
    expect(await screen.findByText('ledger.sqlite3')).toBeInTheDocument()
    expect(screen.getByText('No imports yet')).toBeInTheDocument()
  })

  it('keeps the draft when an upload fails, then clears it on success', async () => {
    loadSetup.mockResolvedValue(SETUP)
    loadImports.mockResolvedValue({ generation: 'g1', imports: [] })
    render(<SetupView onImported={() => {}} />)
    await screen.findByText('ledger.sqlite3')

    const picker = screen.getByLabelText('Statement files') as HTMLInputElement
    const file = new File(['a,b\n1,2\n'], 'jan.csv', { type: 'text/csv' })
    fireEvent.change(picker, { target: { files: [file] } })
    expect(screen.getByText('jan.csv')).toBeInTheDocument()

    submitImport.mockRejectedValueOnce(new ApiError('temporary failure', 503))
    fireEvent.click(screen.getByRole('button', { name: /upload/i }))
    expect(await screen.findByText(/didn’t complete/i)).toBeInTheDocument()
    // Draft survived the failure.
    expect(screen.getByText('jan.csv')).toBeInTheDocument()

    submitImport.mockResolvedValueOnce({ accepted: 1, results: [{ filename: 'jan.csv', adapter: 'REVOLUT', status: 'ACCEPTED', evidence_reused: false }] })
    fireEvent.click(screen.getByRole('button', { name: /upload/i }))
    await waitFor(() => expect(screen.getByText(/1 of 1 file accepted/i)).toBeInTheDocument())
    // The pending-file draft chips are cleared; the filename now only appears in the outcome list.
    expect(document.querySelector('.file-chips')).toBeNull()
    const picker2 = screen.getByLabelText('Statement files') as HTMLInputElement
    expect(picker2.value).toBe('')
  })

  it('rejects an oversize file before upload', async () => {
    loadSetup.mockResolvedValue(SETUP)
    loadImports.mockResolvedValue({ generation: 'g1', imports: [] })
    render(<SetupView onImported={() => {}} />)
    await screen.findByText('ledger.sqlite3')

    const picker = screen.getByLabelText('Statement files') as HTMLInputElement
    const big = new File([new Uint8Array(2)], 'big.csv', { type: 'text/csv' })
    Object.defineProperty(big, 'size', { value: 60 * 1024 * 1024 })
    fireEvent.change(picker, { target: { files: [big] } })
    expect(screen.getByText(/over the 50 MiB limit/i)).toBeInTheDocument()
    expect((screen.getByRole('button', { name: /upload/i }) as HTMLButtonElement).disabled).toBe(true)
  })
})
