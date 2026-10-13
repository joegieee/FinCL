import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { ApiError } from '@/lib/api'

const loadSetup = vi.fn()
const loadImports = vi.fn()
const loadImportDetail = vi.fn()
const submitImport = vi.fn()
const submitFxRates = vi.fn()
const refreshImports = vi.fn()

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    loadSetup: () => loadSetup(),
    loadImports: () => loadImports(),
    loadImportDetail: (id: string) => loadImportDetail(id),
    submitImport: (adapter: string, files: File[]) => submitImport(adapter, files),
    submitFxRates: (file: File, masWeeklyCsv: boolean, sourceVersion: string) => submitFxRates(file, masWeeklyCsv, sourceVersion),
    refreshImports: () => refreshImports(),
  }
})

const { SetupView } = await import('./index')

const SETUP = {
  state: 'ready', current_ledger: 'ledger.sqlite3', storage_profile: 'plaintext-local',
  workspace_root: '/home/user/fincl', can_create: true, can_open: true,
}

afterEach(() => { cleanup(); vi.clearAllMocks() })
beforeEach(() => { refreshImports.mockReset(); refreshImports.mockResolvedValue({}) })

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

    const importWizard = within(document.querySelector('.import-wizard:not(.fx-rates-wizard)') as HTMLElement)
    const picker = screen.getByLabelText('Statement files') as HTMLInputElement
    const file = new File(['a,b\n1,2\n'], 'jan.csv', { type: 'text/csv' })
    fireEvent.change(picker, { target: { files: [file] } })
    expect(screen.getByText('jan.csv')).toBeInTheDocument()

    submitImport.mockRejectedValueOnce(new ApiError('temporary failure', 503))
    fireEvent.click(importWizard.getByRole('button', { name: /upload/i }))
    expect(await screen.findByText(/didn’t complete/i)).toBeInTheDocument()
    // Draft survived the failure.
    expect(screen.getByText('jan.csv')).toBeInTheDocument()

    submitImport.mockResolvedValueOnce({ accepted: 1, results: [{ filename: 'jan.csv', adapter: 'REVOLUT', status: 'ACCEPTED', evidence_reused: false }] })
    fireEvent.click(importWizard.getByRole('button', { name: /upload/i }))
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
    const importWizard = within(document.querySelector('.import-wizard:not(.fx-rates-wizard)') as HTMLElement)
    expect((importWizard.getByRole('button', { name: /upload/i }) as HTMLButtonElement).disabled).toBe(true)
  })

  it.each([
    ['DBS / POSB — PDF statement', 'DBS_CONSOLIDATED'],
    ['DBS PayLah! — PDF statement', 'DBS_PAYLAH'],
    ['MariBank — PDF statement', 'MARI_SAVINGS'],
  ])('uploads %s with the backend adapter and refreshes the ledger', async (label, adapter) => {
    loadSetup.mockResolvedValue(SETUP)
    loadImports.mockResolvedValue({ generation: 'g1', imports: [] })
    submitImport.mockResolvedValue({ accepted: 1, results: [{ filename: 'statement.pdf', adapter, status: 'ACCEPTED', blocking_issue_count: 2 }] })
    const onImported = vi.fn()
    render(<SetupView onImported={onImported} />)
    await screen.findByText('ledger.sqlite3')
    fireEvent.change(screen.getByLabelText('Statement source'), { target: { value: screen.getByRole('option', { name: label }).getAttribute('value') } })
    const file = new File(['%PDF'], 'statement.pdf', { type: 'application/pdf' })
    fireEvent.change(screen.getByLabelText('Statement files'), { target: { files: [file] } })
    fireEvent.click(screen.getByRole('button', { name: 'Upload statements' }))
    await screen.findByText(/1 of 1 file accepted/)
    await waitFor(() => expect(onImported).toHaveBeenCalledOnce())
    expect(submitImport).toHaveBeenCalledWith(adapter, [file])
    expect(refreshImports).toHaveBeenCalledOnce()
    expect(screen.getByText(/2 blocking issues/)).toBeInTheDocument()
  })

  it('retries a failed refresh without uploading accepted statements again', async () => {
    loadSetup.mockResolvedValue(SETUP)
    loadImports.mockResolvedValue({ generation: 'g1', imports: [] })
    submitImport.mockResolvedValue({ accepted: 1, results: [{ filename: 'statement.pdf', adapter: 'REVOLUT_PDF', status: 'ACCEPTED' }] })
    refreshImports.mockRejectedValueOnce(new ApiError('Ledger is busy', 409))
    render(<SetupView onImported={() => {}} />)
    await screen.findByText('ledger.sqlite3')
    fireEvent.change(screen.getByLabelText('Statement files'), { target: { files: [new File(['%PDF'], 'statement.pdf')] } })
    fireEvent.click(screen.getByRole('button', { name: 'Upload statements' }))
    expect(await screen.findByText(/statements were accepted, but the ledger refresh failed/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry ledger refresh' }))
    await waitFor(() => expect(screen.queryByText(/statements were accepted, but the ledger refresh failed/)).not.toBeInTheDocument())
    expect(refreshImports).toHaveBeenCalledTimes(2)
    expect(submitImport).toHaveBeenCalledOnce()
  })

  it('shows a rejected file without refreshing or marking it successful', async () => {
    loadSetup.mockResolvedValue(SETUP)
    loadImports.mockResolvedValue({ generation: 'g1', imports: [] })
    submitImport.mockResolvedValue({ accepted: 0, results: [{ filename: 'bad.pdf', adapter: 'REVOLUT_PDF', status: 'FAILED', error: 'Unsupported statement' }] })
    render(<SetupView onImported={() => {}} />)
    await screen.findByText('ledger.sqlite3')
    fireEvent.change(screen.getByLabelText('Statement files'), { target: { files: [new File(['bad'], 'bad.pdf')] } })
    fireEvent.click(screen.getByRole('button', { name: 'Upload statements' }))
    expect(await screen.findByText('Unsupported statement')).toBeInTheDocument()
    expect(refreshImports).not.toHaveBeenCalled()
  })

  it('loads FX rates from a JSON document', async () => {
    loadSetup.mockResolvedValue(SETUP)
    loadImports.mockResolvedValue({ generation: 'g1', imports: [] })
    render(<SetupView onImported={() => {}} />)
    await screen.findByText('ledger.sqlite3')

    const fxWizard = within(document.querySelector('.fx-rates-wizard') as HTMLElement)
    const picker = fxWizard.getByLabelText('FX rate document') as HTMLInputElement
    const file = new File(['{}'], 'rates.json', { type: 'application/json' })
    fireEvent.change(picker, { target: { files: [file] } })

    submitFxRates.mockResolvedValueOnce({ status: 'LOADED', rates_written: 3 })
    fireEvent.click(fxWizard.getByRole('button', { name: /upload/i }))
    await waitFor(() => expect(fxWizard.getByText(/3 rates loaded/i)).toBeInTheDocument())
    expect(submitFxRates).toHaveBeenCalledWith(file, false, '')
  })

  it('requires a source version before submitting a MAS weekly CSV', async () => {
    loadSetup.mockResolvedValue(SETUP)
    loadImports.mockResolvedValue({ generation: 'g1', imports: [] })
    render(<SetupView onImported={() => {}} />)
    await screen.findByText('ledger.sqlite3')

    const fxWizard = within(document.querySelector('.fx-rates-wizard') as HTMLElement)
    const picker = fxWizard.getByLabelText('FX rate document') as HTMLInputElement
    const file = new File(['csv'], 'rates.csv', { type: 'text/csv' })
    fireEvent.change(picker, { target: { files: [file] } })
    fireEvent.click(fxWizard.getByRole('checkbox', { name: /MAS.*Weekly.*CSV/i }))

    const fxUpload = fxWizard.getByRole('button', { name: /upload/i }) as HTMLButtonElement
    expect(fxUpload.disabled).toBe(true)

    fireEvent.change(fxWizard.getByPlaceholderText('mas-weekly-2026-09'), { target: { value: 'mas-weekly-2026-09' } })
    expect(fxUpload.disabled).toBe(false)
  })
})
