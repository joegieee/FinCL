import { api } from '@/lib/api'

export type Completeness = 'established' | 'provisional' | 'missing'

export type MonthCell = {
  month: string
  completeness: Completeness
  basis: string
  entry_count: number
}

export type Interval = { start: string; end: string }

export type PocketReadiness = {
  account_id: string
  institution: string | null
  display_name: string | null
  source_type: string | null
  currency_pocket: string
  expected_span: { expected_start: string; expected_end: string } | null
  observed_span: { start: string | null; end: string | null }
  span: { start: string; end: string }
  entry_count: number
  reconciliation: { passed: number; total: number }
  months: MonthCell[]
  missing_intervals: Interval[]
  warnings: string[]
}

export type BaselineWindow = {
  quality: 'normal' | 'limited-history' | 'none'
  reason: string
  start_month: string | null
  end_month: string | null
  month_count: number
  basis: 'all-established' | 'some-provisional' | null
}

export type ReadinessTask = {
  kind: 'import-missing' | 'confirm-export-period' | 'resolve-reconciliation' | 'resolve-categorization'
  account_id?: string
  display_name?: string
  currency_pocket?: string
  interval?: Interval | { start: string | null; end: string | null }
  entry_count?: number
  absolute_amount?: string
  note: string
}

export type BaselineReadiness = {
  generation: string
  generated_for: string
  scope: {
    accounts: string[]
    currencies: string[]
    analysis_window: Interval | null
  }
  pockets: PocketReadiness[]
  baseline_window: BaselineWindow
  tasks: ReadinessTask[]
}

export const loadBaselineReadiness = () => api<BaselineReadiness>('baseline/readiness')

export type Ratio =
  | { available: true; numerator: string; denominator: string; value: string }
  | { available: false; reason: string; numerator: string; denominator: string }

export type MonthDecomposition = {
  month: string
  owner_cost_recorded: string
  identified_recurring: string
  unresolved_irregular: string
  adjustments: string
  routine_variable_residual: string
  reconciles: boolean
  income: string
}

export type CurrencyEstimate = {
  months: MonthDecomposition[]
  fixed_recurring_monthly: string
  variable_residual_monthly: string
  variable_residual_median: string
  variable_residual_min: string
  variable_residual_max: string
  irregular_allowance_monthly: string
  routine_lifestyle_baseline: string
  total_observed_monthly: string
  net_received_income_monthly: string
  historical_surplus_monthly: string
  savings_rate: Ratio
  fixed_cost_ratio: Ratio
  roles: {
    months: { month: string; essential: string; discretionary: string; unresolved: string; total: string; reconciles: boolean }[]
    totals: { essential: string; discretionary: string; unresolved: string; total: string; reconciles: boolean }
  } | null
  role_groups: { category_key: string; cost_pattern: string; essential_status: string; tier: number | null; basis: string; review_status: string; amount: string; source_count: number }[]
  reserve:
    | { available: false; reason: string }
    | { available: true; months: string; essential_monthly: string; amount: string; basis: string }
  irregular_risk_entries: { month: string; currency: string; category_key: string; amount: string; threshold: string; review_state: string }[]
  regime: {
    warning: boolean
    reasons: string[]
    leading_empty_months: number
    earlier_mean_owner_cost: string
    recent_mean_owner_cost: string
  }
  sensitivity:
    | { available: false; reason: string }
    | {
        available: true
        basis: string
        not_a_probability: boolean
        excluded_month: string
        excluded_routine_residual: string
        variable_residual_monthly: string
        routine_lifestyle_baseline: string
      }
}

export type IncomeStream = {
  payer: string
  currency: string
  certainty: 'likely'
  occurrence_count: number
  months_seen: string[]
  first_month: string
  last_month: string
  total_received: string
  min_amount: string
  max_amount: string
  note: string
}

export type BaselineEstimate =
  | { available: false; reason: string; baseline_window: BaselineWindow; tasks: ReadinessTask[] }
  | {
      available: true
      generation: string
      baseline_window: BaselineWindow
      currencies: string[]
      sample: { months: string[]; month_count: number; quality: string; method_version: string; residual_method: string; outlier_method: string }
      by_currency: Record<string, CurrencyEstimate>
      income_streams: IncomeStream[]
      reconciliation: Record<string, { balances: boolean; owner_cost_recorded_total: string; owner_cost_baseline_total: string; [k: string]: unknown }>
      limitations: string[]
      tasks: ReadinessTask[]
    }

export const loadBaselineEstimate = () => api<BaselineEstimate>('baseline/estimate')
