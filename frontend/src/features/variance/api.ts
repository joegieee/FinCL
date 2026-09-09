import { api } from '@/lib/api'

export type SnapshotIndex = { snapshots: { snapshot: string; issued_at: string; digest: string; journal_seq: number }[] }
export type VariancePage = {
  snapshot: { snapshot: string; issued_at: string; digest_verified: boolean; case: string }
  as_of: string; currency: string; coverage: { covered: boolean; status: string; reasons: string[] }
  months: { month: string; period_status: string; scored: boolean; net_cash_effect: string | null; unclassified_actual: { receipts: string | null; payments: string | null } }[]
  events: { event_id: string; label: string; expected: string; actual: string | null; status: string; status_reason: string }[]
  unmatched_movements: { movement_id: string; on: string; amount: string; label: string }[]
  biggest_misses: { label: string; expected: string; actual: string; cash_effect: string }[]
  likely_income_realization: { available: boolean; sample_count: number; issued_occurrences: number; occurrence_rate: string | null; expected_amount: string | null; realized_amount: string | null; auto_applied: boolean; reason?: string }
}

export const loadSnapshots = () => api<SnapshotIndex>('forecast/snapshots')
export const saveSnapshot = (generation: string, currency: string, caseName: string) => api<{ snapshot: string; status: string }>('forecast/snapshots', { generation, currency, case: caseName, reason: 'Saved after reviewing the issued forecast' })
export const loadVariance = (snapshot: string, asOf?: string) => api<VariancePage>(`forecast/variance/${encodeURIComponent(snapshot)}${asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''}`)
