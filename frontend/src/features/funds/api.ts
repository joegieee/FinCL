import { reportedApi } from '@/lib/workspaceReporting'
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
  recorded_allocation: {
    allocated_now: string
    entries: { allocation: string; allocated_on: string; amount: string }[]
  }
  insufficient_allocation: { amount: string; visible: boolean; note: string } | null
}
export type FundsPage = {
  generation: string; as_of: string; proposals: FundProposal[]; funds: Fund[]
  cash_separation: { by_currency: Record<string, { available: boolean; total_cash: string | null; earmarked_funds: string; unallocated_cash: string | null; minimum_reserve?: string; reserve_adjusted_unallocated_cash?: string; reason: string | null }> }
  limitations: string[]
}

export const loadFunds = () => reportedApi<FundsPage>('funds')
export const confirmFund = (proposal_id: string, generation: string, reason: string) =>
  api<{ status: string; fund: string }>('funds/confirm', { proposal_id, generation, reason })

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
 * Record a contribution actually set aside for one fund.
 *
 * A contribution is a movement *inside* liquid cash: it raises the fund's
 * earmark and lowers unallocated cash, and it moves no money out. Recording one
 * therefore changes no balance on any day and cannot rescue an insolvent cash
 * path — planning to save is not saving. The due expense is what finally moves
 * cash, exactly once, and releases the earmark.
 */
export const recordAllocation = (
  generation: string,
  fund: string,
  input: { allocated_on: string; amount: string; reason: string },
) => previewThenCommit({
  request_id: crypto.randomUUID(),
  expected_generation: generation,
  kind: 'SINKING_FUND_ALLOCATION_SET',
  anchors: { fund, allocation: crypto.randomUUID() },
  payload: { ...input },
})

/** Withdraw a recorded contribution; the earmark drops back by that amount. */
export const retireAllocation = (
  generation: string,
  fund: string,
  allocation: string,
  reason: string,
) => previewThenCommit({
  request_id: crypto.randomUUID(),
  expected_generation: generation,
  kind: 'SINKING_FUND_ALLOCATION_RETIRE',
  anchors: { fund, allocation },
  payload: { reason },
})
