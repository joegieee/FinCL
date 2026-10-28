import { api } from '@/lib/api'
import type { BatchItem } from '@/lib/command'
import { reportedApi } from '@/lib/workspaceReporting'

// v0.7 trips and country context. Reads go through the reported mirror so
// every total is shown in the reporting currency at its own dates; writes use
// the shared preview/commands/batch endpoints with the native context kinds.

export type Assignment = 'automatic' | 'manual' | 'excluded' | 'ambiguous' | 'none'

export type TripTotals = { gross_cost: string; refunds: string; net_cost: string; entry_count: number }

export type TripSummary = {
  trip_id: string
  name: string
  start_on: string
  end_on: string
  countries: string[]
  reason: string
  entry_count: number
  review_count: number
  totals_by_currency: Record<string, TripTotals>
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
  outside_window?: boolean
}

export type ManualAssignment = { anchor: string; on: string; counted: boolean }

type Envelope = { generation: string; lens: string; lens_note: string; country_list_version: string }
export type TripsPage = Envelope & { trips: TripSummary[] }
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
