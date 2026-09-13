import { api } from '@/lib/api'

export type Occurrence = { anchor: string; date: string; amount: string; description: string; account: string }

export type IncomeCandidate = {
  candidate: string
  merchant_key: string
  currency: string
  account_ids: string[]
  cadence: string
  source_count: number
  first_observed_on: string
  last_receipt_on: string
  confidence: string
  confidence_reason: string
  review_question: string
  amount_variation: {
    basis: string; min_amount: string; max_amount: string
    mean_amount: string; latest_amount: string
  }
  timing_window: {
    cadence: string; first_observed_on: string; last_observed_on: string
    next_expected_between: [string, string]
    day_of_month_min?: number; day_of_month_max?: number
  }
  occurrences: Occurrence[]
}

export type CandidatesPage = {
  generation: string
  candidates: IncomeCandidate[]
  exclusions: { excluded_source_count: number; rule: string }
  policy: string
}

export type InboundRule = {
  rule_id: string
  match: Record<string, unknown>
  classification: string
  reason: string
  journal_seq: number
}

export type InboundRulesPage = { generation: string | null; rules: InboundRule[] }

export type CompiledInboundRule = {
  command: { kind: 'INBOUND_RULE_SET'; anchors: { rule_id: string }; payload: Record<string, unknown> }
  impact: { inbound_matches: number; still_unresolved: number }
}

export const CLASSIFICATIONS = [
  'income', 'reimbursement', 'gift', 'family_transfer', 'self_transfer', 'other',
] as const

export const loadCandidates = () => api<CandidatesPage>('inflows/candidates')
export const loadInboundRules = () => api<InboundRulesPage>('inflows/rules')
export const compileInboundRule = (generation: string, match: Record<string, unknown>, classification: string, reason: string) =>
  api<CompiledInboundRule>('inflows/rules/compile', { generation, match, classification, reason })
