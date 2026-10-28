import { api } from '@/lib/api'
import type { BatchItem } from '@/lib/command'
import { reportedApi } from '@/lib/workspaceReporting'

// Planned flows: spending and income the owner knows is coming. Reads go
// through the reported mirror; every amount and total is worked out on the
// server. Writes use the shared preview/commands/batch endpoints.

export type Direction = 'outflow' | 'inflow'
export type Certainty = 'sure' | 'expected' | 'maybe'
export type Treatment = 'extra' | 'irregular_allowance'
export type OccurrenceState = 'upcoming' | 'partly_matched' | 'matched' | 'overdue' | 'done' | 'cancelled'

export type OccurrenceDraft = { key: string; earliest: string; latest: string; amount: string }

export type PlannedMatch = { entry: string; on: string | null; amount: string; description: string | null; account: string | null; dangling: boolean; reason: string }

export type PlannedOccurrence = {
  key: string
  earliest: string
  latest: string
  on: string | null
  month: string
  planned: string
  matched: string
  remaining: string
  state: OccurrenceState
  overdue: boolean
  outcome: { outcome: 'done' | 'cancelled'; reason: string } | null
  in_forecast: boolean
  forecast: { cases: string[]; amount: string; placements: { earliest: string; latest: string; amount: string; cases: string[]; overdue: boolean; actual: boolean }[] } | null
  forecast_note: string | null
  matches: PlannedMatch[]
  actual_parts: { on: string; amount: string }[]
}

export type PlannedFlow = {
  flow: string
  name: string
  direction: Direction
  currency: string
  account_id: string | null
  account_name: string | null
  category: string | null
  certainty: Certainty
  baseline_treatment: Treatment
  link: { kind: 'trip'; id: string; name: string | null; exists: boolean } | null
  reason: string
  planned: string
  matched: string
  remaining: string
  over_plan: string | null
  next_due: string | null
  occurrences: PlannedOccurrence[]
}

export type PlannedPage = {
  generation: string
  as_of: string
  cutoff: string | null
  reference_day: string
  horizon: { start: string; end: string } | null
  flows: PlannedFlow[]
  months: { month: string; items: { flow: string; occurrence: string }[]; totals: Record<string, { outflows: string; inflows: string }> }[]
  attention: { flow: string; occurrence: string; name: string; direction: Direction; currency: string; latest: string; remaining: string; question: string; trip_linked: boolean }[]
  rules: string[]
}

export type MatchCandidate = { entry: string; on: string; merchant: string; description: string; account: string; amount: string; available: string; suggested_amount: string; matched_here: string | null }
export type MatchCandidates = { generation: string; flow: string; occurrence: string; currency: string; planned: string; remaining: string; window: { from: string; to: string }; candidates: MatchCandidate[] }

export type AccountOption = { id: string; display_name: string; currencies: string[] }
export type TripOption = { trip_id: string; name: string; start_on: string; end_on: string }

export const loadPlanned = () => reportedApi<PlannedPage>('planned')
export const loadCandidates = (flow: string, occurrence: string) =>
  api<MatchCandidates>(`planned/match-candidates?flow=${encodeURIComponent(flow)}&occurrence=${encodeURIComponent(occurrence)}`)
export const loadAccountOptions = () => api<{ accounts: AccountOption[] }>('accounts').then(page => page.accounts)
export const loadTripOptions = () => api<{ trips: TripOption[] }>('trips').then(page => page.trips)

export type ImpactCase = { verdict: string | null; lowest: string | null; lowest_on: string | null; first_short_on: string | null; shortfall: string | null }
export type ImpactAnswer = { answer: string | null; complete: boolean; reporting_currency: string | null; base: ImpactCase; cautious: ImpactCase }
export type ImpactCell = { before: string | null; after: string | null; change: string | null }
export type PlannedImpact = {
  generation: string
  flow: string
  currency: string
  before: ImpactAnswer
  after: ImpactAnswer
  flips: boolean
  months: ({ month: string; currency: string; base: ImpactCell; cautious: ImpactCell })[]
}

export type FlowDraft = {
  name: string
  direction: Direction
  currency: string
  account_id: string | null
  category: string | null
  occurrences: OccurrenceDraft[]
  certainty: Certainty
  baseline_treatment: Treatment
  link: { kind: 'trip'; id: string } | null
  reason: string
}

/** The answer with and without a draft item, worked out by the server. */
export const loadImpact = (flow: string, draft: FlowDraft) => api<PlannedImpact>('planned/impact', { flow, payload: draft })

export const flowSet = (flow: string, draft: FlowDraft) => ({ kind: 'PLANNED_FLOW_SET', anchors: { flow }, payload: { ...draft } })
export const matchSet = (flow: string, occurrence: string, entry: string, amount: string, reason: string) =>
  ({ kind: 'PLANNED_MATCH_SET', anchors: { flow, occurrence, entry }, payload: { amount, reason } })
export const matchRetire = (flow: string, occurrence: string, entry: string, reason: string): BatchItem =>
  ({ kind: 'PLANNED_MATCH_RETIRE', anchors: { flow, occurrence, entry }, payload: { reason } })
export const resolveSet = (flow: string, occurrence: string, outcome: 'done' | 'cancelled', reason: string) =>
  ({ kind: 'PLANNED_OCCURRENCE_RESOLVE_SET', anchors: { flow, occurrence }, payload: { outcome, reason } })
export const resolveRetire = (flow: string, occurrence: string, reason: string) =>
  ({ kind: 'PLANNED_OCCURRENCE_RESOLVE_RETIRE', anchors: { flow, occurrence }, payload: { reason } })
export const flowRetire = (flow: string, reason: string): BatchItem => ({ kind: 'PLANNED_FLOW_RETIRE', anchors: { flow }, payload: { reason } })

/** Everything that must be retired with a flow, matches first, in one batch. */
export function retireFlowItems(flow: PlannedFlow, reason: string): BatchItem[] {
  const matches = flow.link ? [] : flow.occurrences.flatMap(o => o.matches.map(m => matchRetire(flow.flow, o.key, m.entry, reason)))
  return [...matches, flowRetire(flow.flow, reason)]
}

/** A positive amount with at most two decimals, checked as text. */
export function validAmount(value: string): boolean {
  return /^\d+(\.\d{1,2})?$/.test(value) && !/^0+(\.0+)?$/.test(value)
}

function iso(year: number, month: number, day: number): string {
  return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`
}

export function monthWindow(month: string): { earliest: string; latest: string } {
  const [year, number] = month.split('-').map(Number)
  return { earliest: `${month}-01`, latest: iso(year, number, new Date(year, number, 0).getDate()) }
}

/** One row per month from `first`, on the same day, clamped to short months. */
export function monthlyRows(first: string, count: number, amount: string): OccurrenceDraft[] {
  const [year, month, day] = first.split('-').map(Number)
  return Array.from({ length: count }, (_, index) => {
    const total = month - 1 + index
    const y = year + Math.floor(total / 12), m = total % 12 + 1
    const on = iso(y, m, Math.min(day, new Date(y, m, 0).getDate()))
    return { key: String(index + 1), earliest: on, latest: on, amount }
  })
}

export function nextKey(rows: OccurrenceDraft[]): string {
  let next = rows.length + 1
  while (rows.some(row => row.key === String(next))) next += 1
  return String(next)
}

export function monthLabel(month: string): string {
  return new Date(`${month}-01T12:00:00`).toLocaleDateString('en-SG', { month: 'long', year: 'numeric' })
}
