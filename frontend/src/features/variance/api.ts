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
  match_suggestions: MatchSuggestion[]
}

export type MatchCandidate = {
  movement_id: string
  on: string
  amount: string
  label: string
  account: string | null
  amount_difference: string
  exact_amount: boolean
  days_from_window: number
  match_kind: 'full' | 'partial'
  suggested_amount: string
}

export type MatchSuggestion = {
  event_id: string
  label: string
  direction: string
  status: string
  outstanding: string
  window: { earliest: string; latest: string; precision: string }
  candidates: MatchCandidate[]
  rule: string
}

export const loadSnapshots = () => api<SnapshotIndex>('forecast/snapshots')
export const saveSnapshot = (generation: string, currency: string, caseName: string) => api<{ snapshot: string; status: string }>('forecast/snapshots', { generation, currency, case: caseName, reason: 'Saved after reviewing the issued forecast' })
export const loadVariance = (snapshot: string, asOf?: string) => api<VariancePage>(`forecast/variance/${encodeURIComponent(snapshot)}${asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''}`)

async function previewThenCommit(body: {
  request_id: string
  expected_generation: string
  kind: string
  anchors: Record<string, string>
  payload: Record<string, unknown>
}): Promise<{ status: string }> {
  await api('preview', body)
  return api<{ status: string }>('commands', body)
}

/**
 * Bind one actual cash movement to one issued forecast event.
 *
 * This is the decision a suggestion cannot make. Matching is what turns "the
 * salary is missing" into "the salary arrived three days late", and it is the
 * only thing that can feed realization learning, so it is deliberately explicit
 * and journalled — and retractable.
 */
export function recordMatch(
  generation: string,
  snapshot: string,
  input: { event_id: string; movement_id: string; matched_on: string; amount: string; match_kind: 'full' | 'partial'; reason: string },
): Promise<{ status: string }> {
  return previewThenCommit({
    request_id: crypto.randomUUID(),
    expected_generation: generation,
    kind: 'FORECAST_MATCH_SET',
    anchors: { snapshot, match: crypto.randomUUID() },
    payload: { ...input },
  })
}

/** Withdraw a match; the event returns to whatever status the evidence alone supports. */
export function retireMatch(
  generation: string,
  snapshot: string,
  match: string,
  reason: string,
): Promise<{ status: string }> {
  return previewThenCommit({
    request_id: crypto.randomUUID(),
    expected_generation: generation,
    kind: 'FORECAST_MATCH_RETIRE',
    anchors: { snapshot, match },
    payload: { reason },
  })
}
