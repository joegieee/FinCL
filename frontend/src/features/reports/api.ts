import { api } from '@/lib/api'
import { reportedApi } from '@/lib/workspaceReporting'

// v0.7 saved reports. A definition is journal-backed configuration; a result
// is evaluated on every read against the current data and says on which date.

export type GroupKey = 'category' | 'merchant' | 'month' | 'account' | 'trip' | 'country' | 'currency'
export type Column = 'gross_cost' | 'refunds' | 'net_cost' | 'entry_count'
export type Window =
  | { kind: 'all' }
  | { kind: 'fixed'; start: string; end: string }
  | { kind: 'recent_months'; months: number }

export type ReportScope = {
  window: Window
  currencies: string[]
  accounts: string[]
  categories: string[]
  trips: string[]
  countries: string[]
}

export type ReportDefinition = {
  name: string
  scope: ReportScope
  lens: 'recorded_flow'
  group_by: GroupKey[]
  columns: Column[]
}

export type SavedReport = ReportDefinition & { report_id: string; reason: string }

export type ReportRow = {
  group: { key: GroupKey; value: string | null; label: string }[]
  currency: string
  gross_cost: string
  refunds: string
  net_cost: string
  entry_count: number
}

export type ReportResult = {
  generation: string
  evaluated_on: string
  lens: string
  lens_note: string
  report: Partial<SavedReport>
  window: { start: string | null; end: string | null }
  group_labels: Partial<Record<GroupKey, string>>
  warnings: string[]
  rows: ReportRow[]
  totals_by_currency: Record<string, { gross_cost: string; refunds: string; net_cost: string; entry_count: number }>
}

export const GROUP_LABEL: Record<GroupKey, string> = {
  category: 'Category', merchant: 'Merchant', month: 'Month', account: 'Account', trip: 'Trip', country: 'Country', currency: 'Currency',
}
export const COLUMN_LABEL: Record<Column, string> = {
  gross_cost: 'Spent', refunds: 'Refunds', net_cost: 'Net', entry_count: 'Transactions',
}

export const loadReports = () => api<{ reports: SavedReport[] }>('reports')
export const loadResult = (reportId: string) => reportedApi<ReportResult>(`reports/${encodeURIComponent(reportId)}/result`)
export const previewResult = (definition: ReportDefinition) => reportedApi<ReportResult>('reports/preview', definition)
export const loadTripNames = () => api<{ trips: { trip_id: string; name: string }[] }>('trips')
