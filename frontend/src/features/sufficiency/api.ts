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
  by_currency: Record<string, SufficiencyCurrency>
  limitations: string[]
  semantics: Record<string, string>
}

export const loadSufficiency = () => api<SufficiencyPage>('planning/sufficiency')

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
