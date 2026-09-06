import { ArrowDownRight, ArrowUpRight, Minus } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { money, type Analysis } from '@/lib/api'

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

export function ComparisonCards({ analysis, currency }: { analysis: Analysis; currency: string }) {
  const { summary, comparison } = analysis
  return (
    <div className="comparison-grid">
      {CARDS.map(({ key, title, note }) => {
        const cell = summary[key]
        return (
          <Card key={key} className="comparison-card">
            <span className="comparison-label">{title}</span>
            <strong>{money(cell?.value?.value ?? null, currency)}</strong>
            {comparison.available
              ? <DeltaChip delta={cell?.delta?.value ?? null} pct={cell?.pct ?? null} currency={currency} />
              : <span className="delta muted">{comparison.kind === 'none' ? 'Comparison off' : comparison.reasons[0] ?? 'No comparison'}</span>}
            <small>{note}</small>
            {comparison.available && cell?.previous && (
              <small className="comparison-prev">
                was {money(cell.previous.value, currency)} · {comparison.start} to {comparison.end}
              </small>
            )}
          </Card>
        )
      })}
    </div>
  )
}
