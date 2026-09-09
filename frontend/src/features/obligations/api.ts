import { api } from '@/lib/api'

// Stage 2B — the unified obligations read model. Hand-written until the
// integration branch regenerates schema.d.ts from the FastAPI models; the
// `/api/v1/obligations` route returns an untyped mapping today.
export type WireMoney = { value: string; currency: string }

export type ObligationOrigin =
  | 'personal_debt'
  | 'installment_plan'
  | 'card_statement'
  | 'recurring_commitment'

export type AmountBasis = 'remaining_schedule' | 'lump_sum_due' | 'unknown'

export type Confidence = 'confirmed' | 'likely'

export type DateBasis = {
  kind: string
  value: string | null
  schedule?: unknown
}

export type ScheduleRow = {
  due_on: string
  amount: string
  schedule_index?: number
  is_projected?: boolean
}

export type SupersededLumpSum = {
  value: string
  currency: string
  counted: false
  reason: string
}

export type PaymentEntry = {
  amount: string
  effective_on?: string
  scheduled_on?: string
  entry_anchor?: string
  event_kind?: string
  bound?: boolean
}

export type BaselineLink =
  | { available: false; category_key: null; relation: string; reason: string }
  | {
      available: true
      category_key: string
      currency: string
      relation: string
      monthly_baseline_portion: string | null
      portion_unknown_reason: string | null
      note: string
    }

export type Obligation = {
  obligation_id: string
  origin: ObligationOrigin
  source: { kind: string; id: string; account_id?: string }
  label: string
  currency: string
  direction: 'outflow' | 'inflow'
  date_basis: DateBasis
  actual_payments: { count: number; total: string; entries: PaymentEntry[] }
  cash_effect: 'bank_withdrawal' | 'via_card_statement' | 'unknown'
  cash_effect_reason?: string
  statement_payment_due?: string
  includes_installment_occurrences?: {
    obligation_id: string
    effective_on: string
    amount: string
    inside_statement_due: true
  }[]
  unmatched_past_occurrences?: string[]
  baseline_link: BaselineLink
  amount_basis: AmountBasis
  remaining_amount: WireMoney | null
  remaining_unknown_reason: string | null
  schedule: ScheduleRow[] | null
  superseded_lump_sum: SupersededLumpSum | null
  confidence: Confidence
  confidence_basis: string
  status: string
  payment_status: 'evidenced' | 'partially_evidenced' | 'unknown'
  stays_visible: boolean
  overdue: boolean
  basis: string
}

export type CurrencyTotals = {
  bank_withdrawal: string
  via_card_statement: string
  policy: string
}

export type BaselineComponentEffect = {
  currency: string
  category_key: string
  monthly_baseline_portion: string
  replaced_by: string[]
  remaining_historical_portion: string
  relation: string
}

export type ObligationsPage = {
  generation: string
  as_of: string
  obligations: Obligation[]
  totals_by_currency: Record<string, CurrencyTotals>
  baseline: {
    available: boolean
    window: unknown
    month_count: number
    reason: string | null
    basis: string | null
  }
  baseline_component_effects: BaselineComponentEffect[]
  rules: string[]
}

export function loadObligations(opts?: { asOf?: string }): Promise<ObligationsPage> {
  const q = new URLSearchParams()
  if (opts?.asOf) q.set('as_of', opts.asOf)
  const query = q.toString()
  return api<ObligationsPage>('obligations' + (query ? '?' + query : ''))
}
