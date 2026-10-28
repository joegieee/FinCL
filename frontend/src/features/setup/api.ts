import { api, uploadFiles, uploadFxRates } from '@/lib/api'

export type SetupOptions = {
  state: string
  current_ledger: string
  storage_profile: string
  workspace_root: string
  can_create: boolean
  can_open: boolean
}

export type ImportRun = {
  id: string
  started_at: string
  completed_at: string | null
  status: string
  evidence_sha256: string | null
  extraction_run_id: string | null
  extraction_reused: number | null
  error_message: string | null
  byte_size: number | null
  media_type: string | null
  extraction_status: string | null
  selection_state: string | null
  source_profile_version: string | null
  parser_version: string | null
  blockers: number
  filename: string | null
  transaction_count: number | null
  account_id: string | null
  account_display_name: string | null
  period_start: string | null
  period_end: string | null
  statement_state: string | null
  conflict_state: string | null
  /** The period of the statement that already carries every row of this one. */
  superseded_by: string | null
  outcome: 'ACCEPTED' | 'ALREADY_IMPORTED' | 'QUARANTINED' | 'CONFLICT' | 'FAILED' | 'RUNNING' | string
}

export type ImportIssue = { severity: string; code: string; message: string; context: unknown }
export type ConflictingStatement = {
  period_start: string | null
  period_end: string | null
  evidence_sha256: string
  filename: string | null
  imported_at: string | null
  this_import: boolean
}
export type ImportDetail = ImportRun & { issues: ImportIssue[]; conflict_with: ConflictingStatement[] }

export type ImportsPage = { generation: string; imports: ImportRun[] }

export type UploadOutcome = {
  filename: string
  adapter: string | null
  status: string
  /** True when the type was recognised from the file rather than chosen. */
  detected?: boolean
  detection?: string
  evidence_reused?: boolean
  error?: string
  blocking_issue_count?: number
  account_id?: string
  account_display_name?: string
  period_start?: string | null
  period_end?: string | null
  transaction_count?: number
  superseded_by?: string | null
  supersedes?: string[]
}
export type EvidenceThrough = { account_id: string; display_name: string; through: string | null }
export type UploadResult = {
  results: UploadOutcome[]
  accepted: number
  quarantined?: number
  evidence_through?: EvidenceThrough[]
}

// Closed adapter identifiers. Selection is an ID, never a free-text format name.
export const ADAPTERS: { id: string; label: string }[] = [
  { id: 'AUTO', label: 'Recognise automatically (any supported bank)' },
  { id: 'REVOLUT_PDF', label: 'Revolut — PDF statement' },
  { id: 'DBS_CONSOLIDATED', label: 'DBS / POSB — PDF statement' },
  { id: 'DBS_PAYLAH', label: 'DBS PayLah! — PDF statement' },
  { id: 'MARI_SAVINGS', label: 'MariBank — PDF statement' },
  { id: 'MARI_CARD', label: 'Mari Credit Card — PDF statement' },
  { id: 'TRUST_SAVINGS', label: 'Trust Bank — PDF statement' },
]

export const MAX_FILES = 20
export const MAX_BYTES = 50 * 1024 * 1024

export const loadSetup = () => api<SetupOptions>('setup/options')
export const loadImports = () => api<ImportsPage>('imports')
export const loadImportDetail = (id: string) => api<ImportDetail>(`imports/${encodeURIComponent(id)}`)
export const submitImport = (adapter: string, files: File[]) =>
  uploadFiles<UploadResult>('imports', adapter, files)
export const refreshImports = () => api('refresh', {})

export type FxRatesResult = { status: string; rates_written: number }
export const submitFxRates = (file: File, masWeeklyCsv: boolean, sourceVersion: string) =>
  uploadFxRates<FxRatesResult>(file, { mas_weekly_csv: String(masWeeklyCsv), source_version: sourceVersion })
