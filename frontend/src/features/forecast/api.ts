import { api } from '@/lib/api'

export type Timing = {
  precision: 'day' | 'window' | 'unresolved'
  earliest: string | null
  latest: string | null
}

export type Milestone = {
  occurs: boolean
  on: string | null
  precision: Timing['precision']
  window: { earliest: string | null; latest: string | null }
  note: string
  preceded_by_inferred_timing?: boolean
}

export type MonthRow = {
  month: string
  partial: boolean
  days_covered: number
  days_in_month: number
  opening: string
  income: string
  expense: string
  closing: string
  lowest_balance: { amount: string; on: string | null; precision: Timing['precision']; window: { earliest: string; latest: string } }
  closes_positive_over_an_intra_month_low: boolean
}

export type TimelineEvent = {
  event_id: string
  source_id: string
  label: string
  amount: string
  conditional: boolean
  condition: string | null
  timing: { basis: string; earliest: string | null; latest: string | null; precision: Timing['precision'] }
  provenance: Record<string, unknown>
}

export type ForecastCase = {
  available: boolean
  reason?: string
  months: MonthRow[]
  days: { date: string; closing: string }[]
  closing_balance: string
  lowest_balance: Milestone & { amount: string }
  first_zero: Milestone
  first_reserve_breach: (Milestone & { available: true; reserve_target: string }) | { available: false; reason: string }
  policy: { income: string; spending: string; upper_spend_label: string | null }
  events: { items: TimelineEvent[] }
  limitations: string[]
}

export type ForecastCurrency = {
  available: boolean
  readiness?: 'available' | 'partial' | 'unavailable'
  reason?: string
  starting_balance: string
  starting_balance_as_of: string
  forecast_kind?: 'spending-only stress view' | 'cash-flow forecast'
  cash_cutoff?: {
    observation_as_of: string | null
    stale_days: number | null
    missing_intervals: Array<{ start: string; end: string; reason: string }>
    bridge_status: 'supported' | 'partial'
  }
  base: ForecastCase
  conservative: ForecastCase
  reserve_readiness: { available: boolean; target?: string; reason?: string }
}

export type ForecastPage = {
  available: boolean
  readiness?: 'available' | 'partial' | 'unavailable'
  reason?: string
  horizon?: { supported_cash_cutoff: string; end: string; months: number; boundary_rule: string }
  by_currency: Record<string, ForecastCurrency | { available: false; reason: string }>
  case_policy?: { base: string; conservative: string; strong_income_interruption: boolean; strong_income_interruption_note: string | null }
  metadata?: Record<string, string>
  provenance?: Array<{ input: string; module: string; available: boolean; reason: string | null; consumed_by: string[] }>
  source_contracts?: Record<string, unknown>
  action_items?: Array<{
    kind: 'needs_review' | 'missing_evidence' | 'software_limitation'
    output: 'baseline' | 'income' | 'cash' | 'reserve' | 'card_settlement' | 'obligation_timing'
    currency: string | null
    rank: number
    action: string
    detail: string
  }>
  limitations: string[]
}

export const loadForecast = () => api<ForecastPage>('forecast')
