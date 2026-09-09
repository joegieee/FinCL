import { api } from '@/lib/api'

export type NetWorthComponent = {
  component_id: string
  kind: 'cash' | 'debt' | 'card_liability'
  side: 'asset' | 'liability'
  currency: string
  amount: string | null
  as_of: string | null
  basis: 'evidence' | 'manual'
  origin: string
  label: string
  included: boolean
  confidence: string | null
  note: string | null
}

export type MissingComponent = {
  component_id: string
  kind: string
  side: string
  reason: string | null
}

export type ExcludedComponent = MissingComponent & { amount: string }

export type CurrencySummary = {
  tracked_assets: string
  tracked_liabilities: string
  tracked_net_worth: string
  manual_assets: string
  manual_liabilities: string
  net_worth_including_manual: string
  manual_basis_note: string
  subtotal_note: string
  missing_components: MissingComponent[]
  excluded_components: ExcludedComponent[]
  complete: boolean
  valuation_dates: string[]
  as_of: string
}

export type NetWorthTrend = {
  prior_as_of: string
  current_as_of: string
  change_by_currency: Record<string, {
    prior_tracked_net_worth: string | null
    current_tracked_net_worth: string | null
    delta: string
    both_complete: boolean
  }>
  scope_changed: boolean
  scope_changes: { component_id: string; change: string }[]
  comparability: string
}

export type NetWorthPage = {
  as_of: string
  generation: string
  label: string
  is_total_personal_wealth: boolean
  disclaimer: string
  components: NetWorthComponent[]
  notes: string[]
  per_currency: Record<string, CurrencySummary>
  trend?: NetWorthTrend
}

export const loadNetWorth = (params?: { as_of?: string; prior_as_of?: string }) => {
  const query = new URLSearchParams()
  if (params?.as_of) query.set('as_of', params.as_of)
  if (params?.prior_as_of) query.set('prior_as_of', params.prior_as_of)
  const suffix = query.toString()
  return api<NetWorthPage>('net-worth' + (suffix ? `?${suffix}` : ''))
}
