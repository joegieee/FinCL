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
