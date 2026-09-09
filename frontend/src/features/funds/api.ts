import { api } from '@/lib/api'

export type FundProposal = {
  proposal_id: string; label: string; currency: string; complete: boolean
  observed_amount: { value: string; basis: string } | null
  observed_date: { value: string; basis: string; precision: string } | null
  recurrence: { known: boolean; basis: string; note?: string | null }
  incomplete_reasons: string[]; manual_basis: boolean
}
export type Fund = {
  fund: string; name: string; currency: string; status: string; shortfall: string | null
  target: { available: boolean; amount_due: string | null; due_on: string | null; amount_basis: string; date_basis: string }
  planned_contributions: { available: boolean; required_contribution: string | null; remaining_dates: string[]; reason: string | null }
  recorded_allocation: { allocated_now: string }
  insufficient_allocation: { amount: string; visible: boolean; note: string } | null
}
export type FundsPage = {
  generation: string; as_of: string; proposals: FundProposal[]; funds: Fund[]
  cash_separation: { by_currency: Record<string, { available: boolean; total_cash: string | null; earmarked_funds: string; unallocated_cash: string | null; minimum_reserve?: string; reserve_adjusted_unallocated_cash?: string; reason: string | null }> }
  limitations: string[]
}

export const loadFunds = () => api<FundsPage>('funds')
export const confirmFund = (proposal_id: string, generation: string, reason: string) =>
  api<{ status: string; fund: string }>('funds/confirm', { proposal_id, generation, reason })
