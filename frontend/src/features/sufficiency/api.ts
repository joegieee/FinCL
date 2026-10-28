import { reportedApi } from '@/lib/workspaceReporting'
import { api } from '@/lib/api'

export type SufficiencyDriver = {
  origin: string
  label: string
  amount: string
  amount_minor: number
  items: { label: string; on: string; amount: string; conditional: boolean }[]
  item_count: number
}

export type SufficiencyCase = {
  verdict: 'enough' | 'below_reserve' | 'short' | 'unavailable'
  shortfall: string | null
  shortfall_on: string | null
  first_short_on: string | null
  reserve_target: string | null
  drivers: SufficiencyDriver[]
  /** Every outflow family across the horizon, whatever the verdict. */
  accounted_for: SufficiencyDriver[]
  reasons: string[]
  headline: string
}

export type SufficiencyCurrency = {
  currency: string
  scale: number
  answer: string
  verdict: SufficiencyCase['verdict']
  readiness: string | null
  starting_balance: string | null
  starting_balance_as_of: string | null
  horizon_end: string | null
  base: SufficiencyCase
  conservative: SufficiencyCase
  only_safe_in_the_base_case: boolean
}

/** The tabs a "make this answer better" item can send someone to. */
export type SufficiencyDestination = 'Setup' | 'Cards' | 'Debts' | 'Income' | 'Categorization' | 'Cash' | 'Planning'

export type CombinedCase = {
  verdict: SufficiencyCase['verdict']
  lowest: string | null
  lowest_on: string | null
  closing: string | null
  first_short_on: string | null
  shortfall: string | null
  shortfall_on: string | null
}

/** One answer across every pocket, already in the reporting currency. */
export type CombinedAnswer = {
  reporting_currency: string
  answer: string
  verdict: SufficiencyCase['verdict']
  /** False when a pocket could not be counted or a pocket's bills are missing. */
  complete: boolean
  /** Why the answer is incomplete, already worded for the page. */
  incomplete_because: string[]
  starts_on: string | null
  ends_on: string | null
  starting_total: string | null
  base: CombinedCase
  cautious: CombinedCase
  only_safe_in_the_base_case: boolean
  reserve: { available: boolean; target: string | null }
  pockets: {
    currency: string
    counted: 'projected' | 'held_as_is' | 'not_counted' | 'ignored'
    why: string | null
    missing: { kind: string; label: string; reason: string }[]
    starting_balance: string | null
    starting_balance_reported: string | null
    rate: string | null
    rate_display: string | null
  }[]
  income: {
    label: string; currency: string; count: number; next_on: string; confidence: string
    native_total: string; total: string; in_cautious_case: boolean
  }[]
  /** Money that may come in (owed back, uncertain). Listed, never counted. */
  upside: { label: string; currency: string; amount: string; on: string | null }[]
  where_it_goes: {
    origin: string; label: string; total: string; monthly: string
    items: { label: string; on: string; amount: string; conditional: boolean; currency: string }[]
  }[]
  todos: { key: string; page: SufficiencyDestination; title: string; detail: string }[]
  /** Owed money with no due date, so no day of the forecast pays it. */
  not_in_the_path?: {
    as_of: string | null
    total: string
    items: { label: string; origin: string; amount: string; native: { value: string; currency: string } }[]
    unconverted: { label: string; value: string; currency: string }[]
    /** How far below zero the lowest point goes if it were all repaid now; null when nothing is missing. */
    short_if_repaid_now: string | null
  }
}

export type SufficiencyPage = {
  version: string
  generated_for: string
  reserve_policy: {
    months: string
    is_default: boolean
    decided_at: string | null
    reason: string | null
    basis: string
  }
  combined_answer: CombinedAnswer
  by_currency: Record<string, SufficiencyCurrency>
  limitations: string[]
  semantics: Record<string, string>
}

export const loadSufficiency = () => reportedApi<SufficiencyPage>('planning/sufficiency')

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
 * Choose how many months of essential spending to hold back.
 *
 * Only the month count is recorded. The amount it multiplies stays the
 * evidence-derived mean essential spend, so this can never become a way to
 * assert a living cost by hand.
 */
export function setReservePolicy(
  generation: string,
  months: string,
  reason: string,
): Promise<{ status: string }> {
  return previewThenCommit({
    request_id: crypto.randomUUID(),
    expected_generation: generation,
    kind: 'RESERVE_POLICY_SET',
    anchors: { policy: 'reserve' },
    payload: { months, reason },
  })
}
