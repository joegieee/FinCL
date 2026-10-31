import { api } from '@/lib/api'
import type { BatchItem } from '@/lib/command'
import { reportedApi } from '@/lib/workspaceReporting'

// v0.7 trips and country context. Reads go through the reported mirror so
// every total is shown in the reporting currency at its own dates; writes use
// the shared preview/commands/batch endpoints with the native context kinds.

export type Assignment = 'automatic' | 'manual' | 'excluded' | 'ambiguous' | 'none'

export type TripTotals = { gross_cost: string; refunds: string; net_cost: string; entry_count: number }

export type TripStatus = 'upcoming' | 'ongoing' | 'past'

/** What the planned items linked to a trip add up to in one currency, against its spend so far. */
export type TripBudget = {
  currency: string
  as_of: string
  planned: string
  actual: string
  remaining: string
  over_plan: string | null
  over_budget: boolean
  missed: boolean
  unvalued_count: number
  flows: { flow: string; name: string; certainty: string; earliest: string | null; latest: string | null }[]
}

export type TripTodo = { kind: 'assign_trip_spend'; trip_id: string; name: string; count: number; text: string }

export type TripSummary = {
  trip_id: string
  name: string
  start_on: string
  end_on: string
  status: TripStatus
  countries: string[]
  reason: string
  entry_count: number
  review_count: number
  totals_by_currency: Record<string, TripTotals>
  budgets: TripBudget[]
  todo: TripTodo | null
}

export type ContextRow = {
  anchor: string
  on: string
  end: string
  date_precision: string
  merchant: string
  category_key: string
  account: string
  currency: string
  cost_amount: string
  refund_amount: string
  country: string | null
  country_basis: 'operator' | 'unknown'
  assignment: Assignment
  candidates: string[]
  categories: { category_key: string; cost_amount: string; refund_amount: string }[]
  outside_window?: boolean
}

export type ManualAssignment = { anchor: string; on: string; counted: boolean }

type Envelope = { generation: string; lens: string; lens_note: string; country_list_version: string }
export type TripsPage = Envelope & { trips: TripSummary[]; todos: TripTodo[] }
export type TripDetail = Envelope & {
  trip: TripSummary
  by_category: { currency: string; category_key: string; cost_amount: string }[]
  contributors: ContextRow[]
  review: { missing_country: ContextRow[]; ambiguous: ContextRow[]; other_country: ContextRow[]; excluded: ContextRow[] }
  manual_assignments: ManualAssignment[]
}

export const loadTrips = () => reportedApi<TripsPage>('trips')
export const loadTrip = (tripId: string) => reportedApi<TripDetail>(`trips/${encodeURIComponent(tripId)}`)
export const loadCountries = () => api<{ version: string; codes: string[] }>('countries')

export type EntryContext = {
  generation: string
  anchor: string
  counted_as_cost: boolean
  on: string | null
  end: string | null
  country: string | null
  country_basis: 'operator' | 'unknown'
  trip: string | null
  assignment: Assignment
  candidates: string[]
  trips: { trip_id: string; name: string; start_on: string; end_on: string; countries: string[]; in_window: boolean }[]
}
export const loadEntryContext = (anchor: string) => api<EntryContext>(`entry-context?anchor=${encodeURIComponent(anchor)}`)

export type TripDraft = { name: string; start_on: string; end_on: string; countries: string[]; reason: string }

export const tripSet = (trip: string, draft: TripDraft): BatchItem => ({ kind: 'TRIP_SET', anchors: { trip }, payload: { ...draft } })
export const countrySet = (entry: string, country: string | null, reason: string): BatchItem => ({ kind: 'ENTRY_COUNTRY_SET', anchors: { entry }, payload: { country, reason } })
export const tripAssign = (entry: string, trip: string | null, reason: string): BatchItem => ({ kind: 'ENTRY_TRIP_SET', anchors: { entry }, payload: { trip, reason } })
export const tripAssignRetire = (entry: string, reason: string): BatchItem => ({ kind: 'ENTRY_TRIP_RETIRE', anchors: { entry }, payload: { reason } })
export const tripRetire = (trip: string, reason: string): BatchItem => ({ kind: 'TRIP_RETIRE', anchors: { trip }, payload: { reason } })

const regionNames = typeof Intl !== 'undefined' && 'DisplayNames' in Intl ? new Intl.DisplayNames(['en'], { type: 'region' }) : null
export function countryName(code: string): string {
  try { return regionNames?.of(code) ?? code } catch { return code }
}
