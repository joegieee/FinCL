import { reportedApi } from '@/lib/workspaceReporting'
import { api } from '@/lib/api'

export type CommitmentItem = {
  event_id: string
  source_id: string
  label: string
  on: string | null
  amount: string
  cash_effect: 'external_cash_outflow' | 'internal_earmark'
  tier: number | null
  classification_supported: boolean
  edit_href?: string
}

export type CommitmentGroup = {
  amount: string
  cash_effect: 'external_cash_outflow' | 'internal_earmark'
  items: CommitmentItem[]
}

export type PaceBudget = {
  plan: string
  name: string
  category_key: string | null
  limit: string
  actual: string
  remaining: string | null
  status: string
  calendar_elapsed_percent: string
  budget_consumed_percent: string
  coverage: { supported: boolean; badge: string }
  pace_projected_spend: string | null
  pace_estimated_headroom: string | null
}

export type SpendingControlsPage = {
  available: boolean
  reason?: string
  month: string
  currency: string
  horizon?: { supported_cash_cutoff: string; end: string }
  pace: { budgets: PaceBudget[]; coverage_through_observed_date: boolean }
  commitments?: {
    groups: Record<'mandatory_lifestyle' | 'debts_and_cards' | 'important_commitments' | 'planned_allocations', CommitmentGroup>
    external_cash_outflows: string
    internal_earmarks: string
    distinct_source_ids: number
    disjoint: boolean
  }
  free_cash_after_commitments?: { amount: string | null; formula: string; definitive_allowance: boolean }
  support_gates?: Record<string, { supported: boolean; reason: string | null }>
  capacity?: {
    available: boolean
    status: 'unresolved' | 'funding_need' | 'capacity'
    reasons: string[]
    minimum_conservative_unallocated_reserve_adjusted_balance: string | null
    additional_spending_capacity: string | null
    funding_need: string | null
    weekly: null | { amount_per_week: string; entire_path_verified: boolean; schedule: Array<{ on: string; amount: string }> }
  }
  first_breach_drivers?: null | {
    kind: string
    on: string
    events: Array<{ event_id: string; label: string; amount: string; on: string; edit_href: string }>
    later_income_assumptions: Array<{ event_id: string; label: string; amount: string; on: string; assumption: string; edit_href: string }>
  }
  what_if_options: {
    categories: Array<{ category_id: string; category_key: string; tier: number; source_backed_monthly: string; maximum_suggested_monthly_reduction: string }>
    events: Array<{ event_id: string; label: string; amount: string; on: string; direction: string; tier: number | null; protected_tier_1_or_2: boolean }>
  }
  scenario: null | {
    hypothetical: true
    confirmed: false
    statement: string
    comparison: Record<'base' | 'conservative', {
      before_minimum_conservative_unallocated_reserve_adjusted_balance: string
      after_minimum_conservative_unallocated_reserve_adjusted_balance: string
      before_gap: string
      after_gap: string
      gap_improvement: string
      cash_improvement: string
      unchanged_case_preserved: boolean
    }>
    unresolved: string[]
  }
  unresolved?: string[]
  limitations?: string[]
  disclosure?: string
}

export type WhatIfDraft = {
  month: string
  currency: string
  category_cuts: Array<{ category_id: string; monthly_reduction: string }>
  date_changes: Array<{ event_id: string; new_date: string }>
  hypothetical_income: Array<{ amount: string; currency: string; on: string; label: string }>
}

export const loadSpendingControls = (month: string, currency: string) =>
  reportedApi<SpendingControlsPage>(`spending-controls?${new URLSearchParams({ month, currency, include_forecast: 'false' })}`)

export const runSpendingWhatIf = (draft: WhatIfDraft) =>
  reportedApi<SpendingControlsPage>('spending-controls/what-if?include_forecast=false', draft)
