import { api } from '@/lib/api'

// The landing page's steps 1 and 3, in one fast read. Amounts are already in
// the reporting currency, worked out on the server.
export type StatementReach = {
  accounts: { account_id: string; name: string; statements: number; through: string; days_behind: number; behind: boolean }[]
  complete_through: string | null
  latest: string | null
  behind_count: number
  quarantined_count: number
  stale_after_days: number
}

export type OwedGroup = {
  origin: 'personal_debt' | 'card_statement' | 'installment_plan' | 'recurring_commitment'
  label: string
  count: number
  total: string
  by_currency: Record<string, string>
  unknown_amount: string[]
  unconverted: { label: string; value: string; currency: string }[]
  next_due: { label: string; on: string } | null
}

export type MoneyJourney = {
  generated_for: string
  reporting_currency: string
  statements: StatementReach
  owed: {
    as_of: string
    groups: OwedGroup[]
    total: string
    unknown_amount_count: number
    unconverted_count: number
    owed_to_you: { count: number; total: string; unknown: number }
  }
}

export const loadJourney = () => api<MoneyJourney>('journey')

/** The last whole calendar month the statements cover, as [first day, last day]. */
export function lastFullMonth(through: string): [string, string] {
  const [y, m, d] = through.split('-').map(Number)
  const lastDay = new Date(Date.UTC(y, m, 0)).getUTCDate()
  const year = d === lastDay ? y : m === 1 ? y - 1 : y
  const month = d === lastDay ? m : m === 1 ? 12 : m - 1
  const end = new Date(Date.UTC(year, month, 0)).getUTCDate()
  const mm = String(month).padStart(2, '0')
  return [`${year}-${mm}-01`, `${year}-${mm}-${String(end).padStart(2, '0')}`]
}
