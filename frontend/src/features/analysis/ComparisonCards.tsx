import { ArrowDownRight, ArrowUpRight, Minus } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { ConsolidatedMoney } from '@/components/Money'
import { money } from '@/lib/api'
import type { ReportedAnalysis } from './reportedApi'

const CARDS: { key: string; title: string; note: string }[] = [
  { key: 'spending', title: 'Spending', note: 'Gross purchases; fees shown separately' },
  { key: 'fees', title: 'Fees', note: 'Charged fees, a separate series' },
  { key: 'refunds', title: 'Refunds', note: 'Received in this period' },
  { key: 'net_economic_flow', title: 'Recorded net flow', note: 'Classified inflows less spending and fees' },
]

/** Direction of a spending-style delta: more spending is "up" (worse). */
function DeltaChip({ delta, pct, currency }: { delta: string | null; pct: string | null; currency: string }) {
  if (delta === null) return <span className="delta muted">No comparison period</span>
  const n = Number(delta)
  const cls = n > 0 ? 'delta up' : n < 0 ? 'delta down' : 'delta flat'
  const Icon = n > 0 ? ArrowUpRight : n < 0 ? ArrowDownRight : Minus
  return (
    <span className={cls}>
      <Icon size={13} />
      {money(delta.replace('-', ''), currency)}
      {pct !== null && <small>{pct}%</small>}
    </span>
  )
}

/** The four headline comparison cards (§3.1 item 4, §6.3). Every delta/pct
 * here is computed once, server-side, from exact Fractions — never
 * reconstructed from two already-rendered decimal strings in React. */
export function ComparisonCards({ analysis }: { analysis: ReportedAnalysis }) {
  const currency = analysis.reporting_currency
  return (
    <div className="comparison-grid">
      {CARDS.map(({ key, title, note }) => {
        const entry = analysis.summary[key]
        if (!entry) return null
        const previousTotal = entry.previous?.total ?? entry.previous?.partial_total ?? null
        return (
          <Card key={key} className="comparison-card">
            <span className="comparison-label">{title}</span>
            <strong><ConsolidatedMoney
              total={entry.current.total} partialTotal={entry.current.partial_total} currency={currency}
              unavailableReasons={entry.current.unavailable_reasons} excludedDetail={entry.current.excluded_detail}
            /></strong>
            {entry.comparison_available
              ? <DeltaChip delta={entry.delta?.value ?? null} pct={entry.pct} currency={currency} />
              : <span className="delta muted">{analysis.comparison_kind === 'none' ? 'Comparison off' : entry.comparison_reasons[0] ?? 'No comparison'}</span>}
            <small>{note}</small>
            {entry.comparison_available && previousTotal && (
              <small className="comparison-prev">was {money(previousTotal.value, currency)}</small>
            )}
          </Card>
        )
      })}
    </div>
  )
}
