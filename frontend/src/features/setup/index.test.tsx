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

const loadCoverageReport = vi.fn()
const loadBaselineReadiness = vi.fn()

vi.mock('@/features/cash/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/cash/api')>()
  return {
    ...actual,
    loadCoverageReport: () => loadCoverageReport(),
    loadCertifications: () => Promise.resolve({ certifications: [] }),
    loadCashPosition: () => Promise.resolve({ pockets: [] }),
  }
})
vi.mock('@/features/baseline/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/baseline/api')>()
  return { ...actual, loadBaselineReadiness: () => loadBaselineReadiness() }
})

const loadAccounts = vi.fn()
vi.mock('@/features/accounts/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/accounts/api')>()
  return { ...actual, loadAccounts: () => loadAccounts() }
})
const apiMock = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: (path: string, body?: unknown) => apiMock(path, body) }
})

const { SetupView } = await import('./index')

function coverage(over: Record<string, unknown> = {}) {
  return {
    generated_for: '2026-09-20', supported_cutoff: '2026-07-31', current_position_supported: false,
    coverage: { included_pockets: 2, covered_pockets: 1, short_pockets: 1, ratio: 0.5, basis: 'share of included liquid pockets with a coverage-backed balance', status: 'partial' },
    partial_position: { as_of: '2026-08-31', by_currency: { SGD: '10.00' }, contributing_pockets: [], short_pockets: [], is_complete: false, reason: null },
    remedies: [{
      action: 'import-statement', decision_kind: null, account_id: 'acct_revolut_current', currency_pocket: 'SGD',
      display_name: 'Revolut Current', institution: 'Revolut', unlocks: 'unblocks-shared-cutoff', rank: 0,
      detail: 'Import a statement for it.', parameters: {},
    }],
    freshness: [
      { account: 'acct_revolut_current', display_name: 'Revolut Current', currencies: ['SGD'], evidence_through: '2026-07-31', age_days: 51, stale: true, basis: 'statement', holds_back_start: true },
      { account: 'trust-savings', display_name: 'Trust Bank Savings Account', currencies: ['SGD'], evidence_through: '2026-09-20', age_days: 0, stale: false, basis: 'certification', holds_back_start: false },
    ],
    ...over,
  }
}

const SETUP = {
  state: 'ready', current_ledger: 'ledger.sqlite3', storage_profile: 'plaintext-local',
  workspace_root: '/home/user/fincl', can_create: true, can_open: true,
}

afterEach(() => { cleanup(); vi.clearAllMocks() })
beforeEach(() => {
  refreshImports.mockReset(); refreshImports.mockResolvedValue({})
  loadCoverageReport.mockRejectedValue(new Error('no coverage in this test'))
  loadBaselineReadiness.mockRejectedValue(new Error('no readiness in this test'))
  loadAccounts.mockRejectedValue(new Error('no accounts in this test'))
  apiMock.mockReset(); apiMock.mockResolvedValue({ status: 'applied' })
})

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

  it('recognises the bank by default and says which, or asks when it cannot', async () => {
    loadSetup.mockResolvedValue(SETUP)
    loadImports.mockResolvedValue({ generation: 'g1', imports: [] })
    submitImport.mockResolvedValue({ accepted: 1, results: [
      { filename: 'card.pdf', adapter: 'MARI_CARD', status: 'ACCEPTED', detected: true, detection: 'recognised' },
      { filename: 'scan.pdf', adapter: null, status: 'UNRECOGNISED', error: "We couldn't tell which bank this statement is from. Choose its type and upload it again." },
    ] })
    render(<SetupView onImported={() => {}} />)
    await screen.findByText('ledger.sqlite3')
    expect(screen.getByLabelText('Statement source')).toHaveValue('AUTO')
    const files = [new File(['%PDF'], 'card.pdf'), new File(['%PDF'], 'scan.pdf')]
    fireEvent.change(screen.getByLabelText('Statement files'), { target: { files } })
    fireEvent.click(screen.getByRole('button', { name: 'Upload statements' }))
    expect(await screen.findByText('recognised as Mari Credit Card — PDF statement')).toBeInTheDocument()
    expect(screen.getByText('NOT RECOGNISED')).toBeInTheDocument()
    expect(screen.getByText(/Choose its type and upload it again/)).toBeInTheDocument()
    expect(submitImport).toHaveBeenCalledWith('AUTO', files)
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

  it('shows how far each account is proven, what holds the start back and what is missing', async () => {
    loadSetup.mockResolvedValue(SETUP)
    loadImports.mockResolvedValue({ generation: 'g1', imports: [] })
    loadCoverageReport.mockResolvedValue(coverage())
    loadBaselineReadiness.mockResolvedValue({ pockets: [
      { account_id: 'acct_revolut_current', display_name: 'Revolut Current', missing_intervals: [{ start: '2026-08-01', end: '2026-08-31' }] },
    ] })
    render(<SetupView onImported={() => {}} generation="g1" />)

    const card = within(await screen.findByText(/Your forecast starts from/).then(h => h.closest('.setup-coverage') as HTMLElement))
    const revolut = within(card.getByText('Revolut Current').closest('tr') as HTMLElement)
    expect(revolut.getByText('Holding the start back')).toBeInTheDocument()
    expect(revolut.getByText('51 days ago')).toBeInTheDocument()
    expect(revolut.getByText('Aug 2026')).toBeInTheDocument()
    const trust = within(card.getByText('Trust Bank Savings Account').closest('tr') as HTMLElement)
    expect(trust.getByText('a range you certified as quiet')).toBeInTheDocument()
    expect(trust.queryByText('Holding the start back')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Import a statement/ }))
    expect(document.activeElement).toBe(screen.getByLabelText('Statement files'))
  })

  it('offers to close a stopped account, keeping its name, and reloads the card', async () => {
    loadSetup.mockResolvedValue(SETUP)
    loadImports.mockResolvedValue({ generation: 'g1', imports: [] })
    loadCoverageReport.mockResolvedValue(coverage())
    const pocket = { account_id: 'acct_revolut_current', display_name: 'Revolut Current', missing_intervals: [{ start: '2026-08-01', end: '2026-08-31' }] }
    loadBaselineReadiness.mockResolvedValue({ pockets: [{ ...pocket, stopped: true }] })
    loadAccounts.mockResolvedValue({ generation: 'g1', accounts: [{
      id: 'acct_revolut_current', display_name: 'My Revolut', archived_on: '',
      pockets: [{ pocket: 'SGD', observed_end: '2026-07-31' }],
    }] })
    const onImported = vi.fn()
    render(<SetupView onImported={onImported} generation="g1" />)

    fireEvent.click(await screen.findByRole('button', { name: 'This account is closed' }))
    expect(screen.getByLabelText('Closed on')).toHaveValue('2026-07-31')
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Mark closed' }))

    await waitFor(() => expect(onImported).toHaveBeenCalled())
    expect(apiMock).toHaveBeenCalledWith('commands', expect.objectContaining({
      kind: 'ACCOUNT_DETAILS_SET', anchors: { account: 'acct_revolut_current' },
      payload: { display_name: 'My Revolut', archived_on: '2026-07-31', reason: 'Account closed' },
    }))
  })

  it('does not offer to close an account that is only missing a middle month', async () => {
    loadSetup.mockResolvedValue(SETUP)
    loadImports.mockResolvedValue({ generation: 'g1', imports: [] })
    loadCoverageReport.mockResolvedValue(coverage())
    loadBaselineReadiness.mockResolvedValue({ pockets: [
      { account_id: 'acct_revolut_current', display_name: 'Revolut Current', stopped: false, missing_intervals: [{ start: '2026-08-01', end: '2026-08-31' }] },
    ] })
    loadAccounts.mockResolvedValue({ generation: 'g1', accounts: [{ id: 'acct_revolut_current', display_name: 'Revolut Current', pockets: [] }] })
    render(<SetupView onImported={() => {}} generation="g1" />)

    await screen.findByText('Aug 2026')
    expect(screen.queryByRole('button', { name: 'This account is closed' })).toBeNull()
  })

  it('does not count a quarantined file as accepted and says what each file did', async () => {
    loadSetup.mockResolvedValue(SETUP)
    loadImports.mockResolvedValue({ generation: 'g1', imports: [] })
    submitImport.mockResolvedValue({
      accepted: 1, quarantined: 1,
      evidence_through: [{ account_id: 'acct_revolut_current', display_name: 'Revolut Current', through: '2026-05-31' }],
      results: [
        { filename: 'may.pdf', adapter: 'REVOLUT_PDF', status: 'ACCEPTED', account_display_name: 'Revolut Current', period_start: '2026-05-01', period_end: '2026-05-31', transaction_count: 9, superseded_by: null, supersedes: ['2026-05-01..2026-05-15'] },
        { filename: 'trust.pdf', adapter: 'TRUST_SAVINGS', status: 'QUARANTINED', blocking_issue_count: 2 },
      ],
    })
    render(<SetupView onImported={() => {}} />)
    await screen.findByText('ledger.sqlite3')
    fireEvent.change(screen.getByLabelText('Statement files'), { target: { files: [new File(['%PDF'], 'may.pdf'), new File(['%PDF'], 'trust.pdf')] } })
    fireEvent.click(screen.getByRole('button', { name: 'Upload statements' }))

    expect(await screen.findByText(/1 of 2 files accepted\. 1 could not be added\./)).toBeInTheDocument()
    expect(screen.getByText('Revolut Current · 1 May 2026 – 31 May 2026 · 9 rows')).toBeInTheDocument()
    expect(screen.getByText(/Replaces the shorter statement for 1 May 2026 – 15 May 2026/)).toBeInTheDocument()
    expect(screen.getByText('Revolut Current: statements now reach 31 May 2026')).toBeInTheDocument()
    await waitFor(() => expect(refreshImports).toHaveBeenCalledOnce())
  })

  it('lists each import with its file and account, and names both copies of a conflict', async () => {
    const run = {
      id: 'imp_1', started_at: '2026-09-20T01:00:00+00:00', completed_at: null, status: 'SUCCEEDED',
      evidence_sha256: 'b'.repeat(64), extraction_run_id: 'ext_1', extraction_reused: 0, error_message: null,
      byte_size: 2048, media_type: 'application/pdf', extraction_status: 'QUARANTINED', selection_state: 'SELECTED',
      source_profile_version: 'p', parser_version: 'v', blockers: 1, filename: 'corrected.pdf', transaction_count: 4,
      account_id: 'acct_revolut_current', account_display_name: 'Revolut Current', period_start: '2026-01-01',
      period_end: '2026-01-20', statement_state: 'CANDIDATE', conflict_state: 'CONFLICT', superseded_by: null, outcome: 'CONFLICT',
    }
    loadSetup.mockResolvedValue(SETUP)
    loadImports.mockResolvedValue({ generation: 'g1', imports: [run] })
    loadImportDetail.mockResolvedValue({ ...run, issues: [], conflict_with: [
      { period_start: '2026-01-01', period_end: '2026-01-20', evidence_sha256: 'a'.repeat(64), filename: 'january.pdf', imported_at: '2026-09-01T01:00:00+00:00', this_import: false },
      { period_start: '2026-01-01', period_end: '2026-01-20', evidence_sha256: 'b'.repeat(64), filename: 'corrected.pdf', imported_at: '2026-09-20T01:00:00+00:00', this_import: true },
    ] })
    render(<SetupView onImported={() => {}} />)

    const row = within((await screen.findByText('corrected.pdf')).closest('tr') as HTMLElement)
    expect(row.getByText('Revolut Current')).toBeInTheDocument()
    expect(row.getByText('Conflicting copy')).toBeInTheDocument()
    fireEvent.click(row.getByRole('button', { name: 'Open' }))
    expect(await screen.findByText('Two different files claim to be the same statement.')).toBeInTheDocument()
    expect(screen.getByText('january.pdf')).toBeInTheDocument()
    expect(screen.getByText(/\(this import\)/)).toBeInTheDocument()
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
