import { ArrowDownRight, ArrowRight, ArrowUpRight } from 'lucide-react'
import { Card } from '@/components/ui/card'
import { money, shortCategory, type Analysis, type CategoryChange } from '@/lib/api'

function ChangeRow({ row, currency, onDrill }: { row: CategoryChange; currency: string; onDrill: (c: CategoryChange) => void }) {
  const up = Number(row.delta.value) > 0
  return (
    <button className="change-row" onClick={() => onDrill(row)}>
      <span className="change-name">
        {up ? <ArrowUpRight size={14} className="up" /> : <ArrowDownRight size={14} className="down" />}
        {shortCategory(row.category)}
        {row.new_in_period && <small className="tag">new</small>}
      </span>
      <span className="change-values">
        <b>{up ? '+' : '−'}{money(row.delta.value.replace('-', ''), currency)}</b>
        {row.pct !== null
          ? <small>{row.pct}%</small>
          : <small>{Number(row.previous.value) === 0 ? 'no spending before' : '—'}</small>}
      </span>
      <small className="change-detail">
        {money(row.previous.value, currency)} → {money(row.current.value, currency)} <ArrowRight size={11} />
      </small>
    </button>
  )
}

export function ChangeRanking({
  analysis, currency, onDrill,
}: {
  analysis: Analysis
  currency: string
  onDrill: (c: CategoryChange) => void
}) {
  const { comparison, category_changes } = analysis
  if (!comparison.available) {
    return (
      <Card className="ranking">
        <h2>What changed?</h2>
        <div className="empty">
          <p>{comparison.kind === 'none' ? 'Turn on a comparison period to see category changes.' : comparison.reasons[0]}</p>
        </div>
      </Card>
    )
  }
  return (
    <Card className="ranking change-ranking">
      <h2>What changed?</h2>
      <p className="restate-note">Restated using current categories · vs {comparison.start} to {comparison.end}</p>
      <div className="change-columns">
        <div>
          <h3>Biggest increases</h3>
          {category_changes.increases.length
            ? category_changes.increases.slice(0, 8).map(r => (
                <ChangeRow key={r.category} row={r} currency={currency} onDrill={onDrill} />
              ))
            : <p className="empty-line">No categories rose.</p>}
        </div>
        <div>
          <h3>Biggest decreases</h3>
          {category_changes.decreases.length
            ? category_changes.decreases.slice(0, 8).map(r => (
                <ChangeRow key={r.category} row={r} currency={currency} onDrill={onDrill} />
              ))
            : <p className="empty-line">No categories fell.</p>}
        </div>
      </div>
    </Card>
  )
}
