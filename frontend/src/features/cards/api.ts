import { reportedApi } from '@/lib/workspaceReporting'
import { api } from '@/lib/api'

// v0.4 card read shapes. Hand-written until the integration branch regenerates
// schema.d.ts from the FastAPI models (release doc §3.5).
export type WireMoney = { value: string; currency: string | null }
export type FactShape = {
  role: string
  kind: 'money' | 'date'
  value: string
  currency: string | null
  source_locator: string
  parser_version: string
  role_version: string
}

export type CardAccount = {
  account_id: string
  institution: string
  source_type: string
  display_name: string
  masked_identifier: string
}

export type LatestStatement = {
  statement_id: string
  statement_date: string
  period_end: string | null
  closing_liability: FactShape | null
  payment_due: FactShape | null
  minimum_payment: FactShape | null
  payment_due_date: FactShape | null
  credit_limit: FactShape | null
  reconciliation_status: string
}

export type CardListRow = {
  account: CardAccount
  latest_statement: LatestStatement | null
  statement_count: number
  coverage_gaps: { from: string; to: string }[]
  statements_missing_period: string[]
  installment_obligations: unknown[]
}

export type CardsPage = {
  generation: string
  as_of: string
  cards: CardListRow[]
  next_cursor: string | null
}

export type PocketView = {
  currency: string
  opening_liability: FactShape | null
  closing_liability: FactShape | null
  summary_buckets: Record<string, FactShape>
  due_fields: Record<string, FactShape>
  credit_limit: FactShape | null
  reconciliation: {
    status: string
    printed_equation: string | null
    blocking_total: number
    blocking_passed: number
    checks: {
      check_type: string
      passed: boolean | null
      blocking: boolean
      expected: string | null
      actual: string | null
      difference: string | null
    }[]
  }
  fields_present: string[]
}

export type StatementView = {
  statement_id: string
  statement_identity: string
  statement_date: string
  period_start: string | null
  period_end: string | null
  evidence_sha256: string
  pockets: PocketView[]
}

export type CardStatementsPage = {
  generation: string
  account: CardAccount
  statements: StatementView[]
  coverage_gaps: { from: string; to: string }[]
  statements_missing_period: string[]
  next_cursor: string | null
}

export type Utilization =
  | { available: false; reason: string }
  | {
      available: true
      basis: string
      currency: string
      credit_balance: boolean
      credit_balance_amount?: string
      ratio: string
      over_limit: boolean
      over_limit_amount?: string | null
      headroom?: string
    }

export type CardDetail = {
  generation: string
  as_of: string
  account: CardAccount
  facility: {
    facility_id: string
    label: string
    currency: string
    bound: boolean
    account_ids?: string[]
  } | null
  limit_basis:
    | (FactShape & { observed_on?: string; source_kind?: string })
    | { kind: 'money'; value: string; currency: string; observed_on: string; source_kind: string }
    | null
  limit_observation: {
    observation_id: string
    observed_on: string
    limit: WireMoney
    source_ref: string | null
    source_kind: string
  } | null
  /** How you said this card is repaid; null when nothing is declared. */
  repayment_plan?: { rule: 'pay_in_full'; reason: string; journal_seq: number } | null
  /** Each statement's amount due and what was paid toward it, newest first. */
  repayments?: {
    statement_id: string
    statement_date: string
    currency: string
    due: string
    paid: string
    payment_due_date: string | null
    paid_basis: 'manual_allocation' | 'card_credits_in_payment_window' | 'none'
    paid_in_full: boolean
  }[]
  utilization: Utilization
  latest_statement: LatestStatement | null
  statement_activity: StatementView[]
  coverage_gaps: { from: string; to: string }[]
  statements_missing_period: string[]
  manually_allocated_payments: {
    card_entry_anchor: string
    statement_id: string
    amount: WireMoney
    manual: boolean
    bound: boolean
    dangling_reason: string | null
  }[]
}

export const RECONCILIATION_LABEL: Record<string, string> = {
  reconciled: 'Reconciled against the printed source equation',
  failed: 'One or more printed equations did not reconcile',
  unverified: 'Equation recognized but not verified',
  no_equation: 'Printed summary equation not recognized',
}

export function loadCards(opts: { asOf?: string; generation: string }): Promise<CardsPage> {
  const q = new URLSearchParams({ generation: opts.generation, limit: '200' })
  if (opts.asOf) q.set('as_of', opts.asOf)
  return reportedApi<CardsPage>('cards?' + q)
}

export function loadCardDetail(
  id: string,
  opts: { asOf?: string; generation: string },
): Promise<CardDetail> {
  const q = new URLSearchParams({ generation: opts.generation })
  if (opts.asOf) q.set('as_of', opts.asOf)
  return reportedApi<CardDetail>(`cards/${encodeURIComponent(id)}?` + q)
}

export function loadCardStatements(
  id: string,
  opts: { generation: string },
): Promise<CardStatementsPage> {
  const q = new URLSearchParams({ generation: opts.generation, limit: '200' })
  return reportedApi<CardStatementsPage>(`cards/${encodeURIComponent(id)}/statements?` + q)
}
