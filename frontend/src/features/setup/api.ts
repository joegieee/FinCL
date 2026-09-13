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
}

export type ImportIssue = { severity: string; code: string; message: string; context: unknown }
export type ImportDetail = ImportRun & { issues: ImportIssue[] }

export type ImportsPage = { generation: string; imports: ImportRun[] }

export type UploadOutcome = {
  filename: string
  adapter: string
  status: string
  evidence_reused?: boolean
  error?: string
  blockers?: number
}
export type UploadResult = { results: UploadOutcome[]; accepted: number }

// Closed adapter identifiers. Selection is an ID, never a free-text format name.
export const ADAPTERS: { id: string; label: string }[] = [
  { id: 'REVOLUT_PDF', label: 'Revolut — PDF statement' },
  { id: 'DBS', label: 'DBS / POSB — PDF statement' },
  { id: 'PAYLAH', label: 'DBS PayLah! — PDF statement' },
  { id: 'MARIBANK', label: 'MariBank — PDF statement' },
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

export type FxRatesResult = { status: string; rates_written: number }
export const submitFxRates = (file: File, masWeeklyCsv: boolean, sourceVersion: string) =>
  uploadFxRates<FxRatesResult>(file, { mas_weekly_csv: String(masWeeklyCsv), source_version: sourceVersion })
