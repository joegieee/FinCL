import { api } from '@/lib/api'

export type BudgetCard = {
  plan: string
  name: string
  currency: string
  category_key: string | null
  effective_month: string
  limit: string
  actual: string
  remaining: string | null
  archived: boolean
  status: 'settled' | 'observed_so_far' | 'coverage_unknown' | 'archived'
}
export type BudgetsPage = {
  generation: string
  month: string
  currency: string
  month_covered: boolean
  month_complete: boolean
  budgets: BudgetCard[]
}

export type BudgetRevision = {
  revision: string
  effective_month: string
  name: string
  currency: string
  category_key: string | null
  limit: string
  journal_seq: number
}
export type BudgetHistory = {
  generation: string
  plan: string
  revisions: BudgetRevision[]
  archive_periods: { effective_month: string; archived: boolean; journal_seq: number }[]
}

export type GoalCard = {
  plan: string
  name: string
  currency: string
  target: string
  target_on: string | null
  observed_on: string | null
  saved: string | null
  remaining: string | null
  dated_progress_known: boolean
  archived: boolean
  overdue: boolean
}
export type GoalsPage = { generation: string; as_of: string; goals: GoalCard[] }

export type Commitment = {
  commitment: string
  name: string
  currency: string
  merchant_key: string
  status: 'active' | 'paused' | 'ended'
  ended_on: string | null
  schedule: { kind: 'weekly' | 'monthly'; day?: number; weekday?: number; anchor_on: string }
  account_ids: string[]
  expected_amount: string
  amount_tolerance: string
}
export type RecurringPage = {
  generation: string
  confirmed: Commitment[]
  paused_or_ended: Commitment[]
  dismissed_candidate_keys: string[]
}
export type Occurrence = { scheduled_on: string; state: string; entry: string | null }
export type OccurrencesPage = { generation: string; commitment: string; occurrences: Occurrence[] }

export const loadBudgets = (month: string, currency: string) =>
  api<BudgetsPage>(`budgets?month=${encodeURIComponent(month)}&currency=${encodeURIComponent(currency)}`)
export const loadBudgetHistory = (plan: string) =>
  api<BudgetHistory>(`budgets/${encodeURIComponent(plan)}/history`)
export const loadGoals = (asOf?: string) =>
  api<GoalsPage>(`goals${asOf ? `?as_of=${encodeURIComponent(asOf)}` : ''}`)
export const loadRecurring = () => api<RecurringPage>('recurring')
export const loadOccurrences = (commitment: string, start: string, end: string) =>
  api<OccurrencesPage>(`recurring/${encodeURIComponent(commitment)}/occurrences?start=${start}&end=${end}`)

export function thisMonth(): string {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
}
